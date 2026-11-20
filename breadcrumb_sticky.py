import os
import sublime
import sublime_plugin
from html import escape

# -------------------------
# LSP AVAILABILITY CHECK
# -------------------------
try:
    from LSP.plugin.core.registry import windows as lsp_windows
    from LSP.plugin.core.protocol import Request as LspRequest
    from LSP.plugin.core.views import text_document_identifier
    _LSP_AVAILABLE = True
except ImportError:
    print("[Breadcrumb] LSP package not found — symbol context unavailable.")
    _LSP_AVAILABLE = False


# Delay before asking the language server for fresh symbols after an edit.
SYMBOL_REQUEST_DEBOUNCE_MS = 300

# view.id() -> (change_count, symbols) — last documentSymbol response per view
_symbol_cache = {}
# view.id() -> change_count currently being requested
_pending_requests = {}
# view.id() -> int — generation counter used to debounce symbol requests
_debounce_gen = {}
# view.id() -> (html, location) — what the breadcrumb popup currently shows
_shown = {}


# -------------------------
# FILE PATH BREADCRUMB
# -------------------------
def format_breadcrumb(path):
    if not path:
        return "No file"
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = [p for p in path.split(os.sep) if p]
    return " › ".join(parts[-5:])


# -------------------------
# SYMBOL HIERARCHY
# -------------------------
def find_symbol_path(symbols, line: int, path: list):
    for s in symbols:
        rng = s.get("range") or (s.get("location") or {}).get("range")
        if not rng:
            continue

        start = rng["start"]["line"]
        end = rng.get("end", rng["start"])["line"]

        if start <= line <= end:
            path.append(s.get("name", "?"))
            children = s.get("children", [])
            if children:
                find_symbol_path(children, line, path)
            break


def current_context(view):
    """Symbol path at the cursor, computed from cached symbols (no LSP call)."""
    cached = _symbol_cache.get(view.id())
    if not cached or not view.sel():
        return []
    _, symbols = cached
    path = []
    if isinstance(symbols, list):
        row, _ = view.rowcol(view.sel()[0].begin())
        find_symbol_path(symbols, row, path)
    return path


# -------------------------
# SHOW POPUP
# -------------------------
def build_html(breadcrumb, context):
    context_html = "".join(f" › <em>{escape(c)}</em>" for c in context)
    return f"""
    <body id="breadcrumb" style="margin:0;padding:0;">
        <div style="white-space:nowrap;padding:4px 8px;font-size:0.9rem;
                    background-color:var(--background);
                    border-bottom:1px solid color(var(--foreground) alpha(0.15));">
            📂 {escape(breadcrumb)}{context_html}
        </div>
    </body>
    """


def render(view, force=False):
    """Show the breadcrumb popup, touching it only when something changed."""
    if not view.is_valid():
        return
    html = build_html(format_breadcrumb(view.file_name() or ""), current_context(view))
    location = view.visible_region().begin()
    key = (html, location)
    visible = view.is_popup_visible()
    last = _shown.get(view.id())

    if visible and not force and last == key:
        return  # same location, already on screen — nothing to do

    if visible and not force and last is not None and last[1] == location:
        view.update_popup(html)  # in-place content swap, no hide/show flash
    else:
        view.show_popup(
            html,
            location=location,
            max_width=2000,
            flags=sublime.COOPERATE_WITH_AUTO_COMPLETE | sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        )
    _shown[view.id()] = key


# -------------------------
# LSP SYMBOL FETCHING
# -------------------------
def find_session(view):
    wm = lsp_windows.lookup(view.window())
    if not wm:
        return None
    session = None
    for s in wm.get_sessions():
        if s.has_capability("documentSymbolProvider"):
            if s.config.selector and view.match_selector(0, s.config.selector):
                return s
            session = s  # fallback
    return session


def symbols_up_to_date(view):
    cached = _symbol_cache.get(view.id())
    return cached is not None and cached[0] == view.change_count()


def request_symbols(view):
    """Ask the language server for symbols unless cache/in-flight request is current."""
    if not _LSP_AVAILABLE or not view.is_valid():
        return
    vid = view.id()
    change_count = view.change_count()
    if symbols_up_to_date(view) or _pending_requests.get(vid) == change_count:
        return

    try:
        session = find_session(view)
        if not session:
            return

        def on_response(symbols):
            if _pending_requests.get(vid) == change_count:
                del _pending_requests[vid]
            _symbol_cache[vid] = (change_count, symbols)
            sublime.set_timeout(lambda: _refresh_if_active(view), 0)

        def on_error(err):
            if _pending_requests.get(vid) == change_count:
                del _pending_requests[vid]
            print(f"[Breadcrumb] LSP error: {err}")

        _pending_requests[vid] = change_count
        params = {"textDocument": text_document_identifier(view)}
        session.send_request(
            LspRequest.documentSymbols(params, view), on_response, on_error
        )
    except Exception as e:
        _pending_requests.pop(vid, None)
        print(f"[Breadcrumb] Error: {e}")
        import traceback
        traceback.print_exc()


def schedule_symbol_request(view):
    """Debounced request_symbols, so typing doesn't fire a request per keystroke."""
    vid = view.id()
    gen = _debounce_gen.get(vid, 0) + 1
    _debounce_gen[vid] = gen

    def fire():
        if _debounce_gen.get(vid) == gen:
            request_symbols(view)

    sublime.set_timeout(fire, SYMBOL_REQUEST_DEBOUNCE_MS)


def _is_active(view):
    return (
        view
        and view.is_valid()
        and view.window()
        and view == view.window().active_view()
    )


def _refresh_if_active(view):
    if _is_active(view):
        render(view)


# -------------------------
# COMMAND
# -------------------------
class BreadcrumbRefreshCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        _symbol_cache.pop(view.id(), None)  # manual refresh: drop cached symbols
        render(view, force=True)
        request_symbols(view)


# -------------------------
# EVENT LISTENER
# -------------------------
class BreadcrumbListener(sublime_plugin.EventListener):
    def _show_and_fetch(self, view):
        if not _is_active(view):
            return
        render(view)
        request_symbols(view)

    def on_activated_async(self, view):
        sublime.set_timeout(lambda: self._show_and_fetch(view), 100)

    def on_load_async(self, view):
        sublime.set_timeout(lambda: self._show_and_fetch(view), 100)

    def on_post_save_async(self, view):
        sublime.set_timeout(lambda: self._show_and_fetch(view), 100)

    def on_selection_modified(self, view):
        # Runs on the main thread so an unchanged breadcrumb is a no-op and a
        # changed one is updated in place, using cached symbols (no LSP call).
        if not _is_active(view):
            return
        render(view)
        if not symbols_up_to_date(view):
            schedule_symbol_request(view)

    def on_close(self, view):
        vid = view.id()
        for d in (_symbol_cache, _pending_requests, _debounce_gen, _shown):
            d.pop(vid, None)


# -------------------------
# PLUGIN LIFECYCLE
# -------------------------
def plugin_loaded():
    print("[Breadcrumb] Plugin loaded" + ("" if _LSP_AVAILABLE else " (no LSP)"))

def plugin_unloaded():
    print("[Breadcrumb] Plugin unloaded")
