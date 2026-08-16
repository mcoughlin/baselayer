from baselayer.app.handlers.base import BaseHandler


class MainPageHandler(BaseHandler):
    def get(self):
        # Never cache the SPA shell: a stale copy points at bundle hashes that no
        # longer exist after a rebuild, 404-ing lazy chunks (ChunkLoadError).
        self.set_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if not self.current_user:
            self.render("login.html")
        else:
            self.render("index.html")
