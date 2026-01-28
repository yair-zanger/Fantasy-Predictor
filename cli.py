#!/usr/bin/env python3
"""
Fantasy Basketball Predictor - Command Line Interface
Use this for quick predictions without the web UI
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yahoo_auth import auth
from yahoo_api import api
from predictor import predictor


def main():
    """Run CLI prediction"""
    print("\n" + "="*60)
    print("🏀 Fantasy Basketball Predictor - CLI")
    print("="*60 + "\n")
    
    # Check authentication
    if not auth.is_authenticated():
        print("🔐 צריך להתחבר ל-Yahoo Fantasy...")
        if not auth.authenticate_interactive():
            print("❌ ההתחברות נכשלה")
            return
        print()
    
    # Get leagues
    print("📋 מביא את הליגות שלך...")
    try:
        leagues = api.get_user_leagues()
    except Exception as e:
        print(f"❌ שגיאה בקבלת הליגות: {e}")
        return
    
    if not leagues:
        print("❌ לא נמצאו ליגות")
        return
    
    # Show leagues
    print("\nהליגות שלך:")
    print("-" * 40)
    for i, league in enumerate(leagues, 1):
        print(f"{i}. {league['name']} (שבוע {league['current_week']})")
    
    # Select league
    print()
    choice = input("בחר ליגה (מספר): ").strip()
    try:
        league_idx = int(choice) - 1
        if league_idx < 0 or league_idx >= len(leagues):
            raise ValueError()
        selected_league = leagues[league_idx]
    except:
        print("❌ בחירה לא חוקית")
        return
    
    # Generate prediction
    print(f"\n📊 מייצר חיזוי ל-{selected_league['name']}...")
    print("-" * 60)
    
    try:
        prediction = predictor.predict_matchup(selected_league['league_key'])
        report = predictor.format_prediction_report(prediction)
        print(report)
    except Exception as e:
        print(f"❌ שגיאה בחיזוי: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
