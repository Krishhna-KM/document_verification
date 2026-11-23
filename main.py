"""Entry point:  python main.py"""
import sys

try:
    from backend.app import create_app
    from backend.config import settings
except Exception as e:  # a bad value in .env should say so plainly, not dump a traceback
    if type(e).__name__ == "SettingsError":
        sys.exit(f"Configuration error: {e}")
    raise

app = create_app()

if __name__ == "__main__":
    app.run(host=settings.HOST, port=settings.PORT, debug=settings.DEBUG)
