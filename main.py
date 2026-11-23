"""Entry point:  python main.py"""
import os
import socket
import sys

try:
    from backend.app import create_app
    from backend.config import settings
except Exception as e:  # a bad value in .env should say so plainly, not dump a traceback
    if type(e).__name__ == "SettingsError":
        sys.exit(f"Configuration error: {e}")
    raise


def _check_address(host: str, port: int) -> None:
    """Fail with a readable message if HOST:PORT cannot be listened on (wrong IP, port already in use)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
    except OSError as e:
        try:
            mine = sorted(set(socket.gethostbyname_ex(socket.gethostname())[2]))
        except OSError:
            mine = []
        sys.exit(
            f"Cannot listen on {host}:{port} - {e.strerror or e}.\n"
            f"  - If HOST is an IP address, this computer no longer has it (DHCP addresses change). "
            f"Set HOST=0.0.0.0 in .env to listen on every address, or HOST=127.0.0.1 for this computer only.\n"
            f"  - If the port is busy, stop the other program or change PORT in .env.\n"
            f"  This computer's addresses: {', '.join(mine) or 'unknown'}")


app = create_app()

if __name__ == "__main__":
    # with the debug reloader this file runs twice; only check in the parent process
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _check_address(settings.HOST, settings.PORT)
    app.run(host=settings.HOST, port=settings.PORT, debug=settings.DEBUG)
