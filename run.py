#!/usr/bin/env python3
"""
Fantasy Basketball Predictor - Entry Point
Run this file to start the application
"""
import sys
import os

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_dependencies():
    """Check if required packages are installed"""
    required = ['flask', 'requests', 'pandas', 'numpy']
    missing = []
    
    for package in required:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)
    
    if missing:
        print("[X] Missing required packages:")
        print(f"   {', '.join(missing)}")
        print("\n[i] To install, run:")
        print("   pip install -r requirements.txt")
        return False
    
    return True


def main():
    """Main entry point"""
    print("\n" + "="*60)
    print("Fantasy Basketball Predictor")
    print("   Yahoo Fantasy Basketball Matchup Predictions")
    print("="*60 + "\n")
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Import and run the app
    from app import app
    
    print("[OK] Starting server...")
    print("[>] Open browser at: https://localhost:5000")
    print("\n[!] To stop: Ctrl+C")
    print("="*60 + "\n")
    
    # Run with SSL for Yahoo OAuth
    app.run(debug=True, port=5000, host='0.0.0.0', ssl_context='adhoc')


if __name__ == '__main__':
    main()
