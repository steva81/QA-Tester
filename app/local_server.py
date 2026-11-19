"""A desktop window must own the loopback listener it displays."""
import socket

from werkzeug.serving import BaseWSGIServer


class LocalServerStartError(RuntimeError):
    pass


class DesktopWSGIServer(BaseWSGIServer):
    # Windows SO_REUSEADDR permits two live listeners on the same address.
    allow_reuse_address = False

    def server_bind(self):
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
        except OSError as exc:
            # Werkzeug otherwise calls sys.exit before desktop's error dialog.
            raise LocalServerStartError(
                f'QA Tester could not open 127.0.0.1:{self.port}. '
                'The port may already be in use. Close the other QA Tester '
                'window or choose a different port, then try again.'
            ) from exc


def make_desktop_server(port, app):
    return DesktopWSGIServer('127.0.0.1', port, app)
