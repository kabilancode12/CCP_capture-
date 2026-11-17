"""The explorer website: routes, login, access rules and the handlers that fill the pages.

``create_app`` returns a Starlette application meant to be mounted at ``/explorer`` (or served on
its own). Every link is built from the request's ``root_path``, so the mount point does not matter.

How a request is handled: the handler runs in a worker thread with one database connection (opened
for the request, committed at the end or rolled back on an error), after the session cookie has been
checked and, for a POST, the CSRF token. Who may see what:

=========  ==================================================================================
Role       Chats
=========  ==================================================================================
developer  full content of the chats of their linked Claude account; for others only counts
           (opening another account's chat gives 404, as if it did not exist)
lead       every chat, may approve or reject decisions
auditor    like a lead, and reads the access log
admin      everything, and manages users and labels
=========  ==================================================================================

Every chat page that is opened writes one row to ``ui_access_log`` (action ``view_chat``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode

import pg8000.dbapi
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from ccp.catalog.builder import report_html_of_chat, report_of_chat
from ccp.db.connection import connect
from ccp.db.labels import LabelError, remove_label, set_account_label, set_project_label
from ccp.explorer import library_views, pages, queries, users
from ccp.explorer.security import PasswordError, SessionData, SessionSigner, verify_password
from ccp.explorer.users import UiUser, UserError
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore

logger = logging.getLogger("ccp.explorer")

COOKIE = "ccp_explorer"
SESSION_SECONDS = 8 * 3600
MAX_FORM_BYTES = 64 * 1024
MAX_NOTE_CHARS = 500

# Failed logins allowed per e-mail address in the window, to slow down guessing.
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 600

# What the redirect after an admin action says: fixed texts, so the address cannot inject text.
_DONE = {
    "created": "User created.",
    "role": "Role changed.",
    "account": "Account link changed.",
    "disabled": "User disabled.",
    "enabled": "User enabled.",
    "password": "Password reset. Open sessions of that user have ended.",
    "label": "Label saved.",
    "unlabel": "Label removed.",
}

_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Context:
    """Everything a handler needs: the request, its form, the connection and the visitor."""

    request: Request
    form: dict[str, str]
    connection: pg8000.dbapi.Connection
    base: str
    session: SessionData | None
    user: UiUser | None
    csrf: str
    new_session: SessionData | None = None
    clear_session: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def view(self) -> pages.View:
        """The page helper for this visitor."""
        return pages.View(self.base, self.user, self.csrf)

    def link(self, path: str, **query: object) -> str:
        """An address inside the site, for a redirect (not HTML-escaped)."""
        pairs = {k: str(v) for k, v in query.items() if v is not None}
        return self.base + path + (f"?{urlencode(pairs)}" if pairs else "")

    def redirect(self, path: str, **query: object) -> Response:
        """Send the browser to another page (303, so a POST is not repeated)."""
        return RedirectResponse(self.link(path, **query), status_code=303)

    def page(self, text: str, status: int = 200) -> Response:
        """An HTML response."""
        return HTMLResponse(text, status_code=status)

    def query(self, name: str) -> str | None:
        """A query-string value, or ``None``."""
        return self.request.query_params.get(name)


Handler = Callable[[Context], Response]


class ExplorerApp:
    """The website's state (settings, signer, login attempts) and its handlers."""

    def __init__(self, database: DatabaseSettings, store: ObjectStore | None, secret: str) -> None:
        """Create the site.

        Args:
            database: The database to read and write.
            store: The object store of the raw transcripts. Reserved: every page is built from
                the index in PostgreSQL, so nothing reads it yet.
            secret: The secret that signs cookies and form tokens (``CCP_EXPLORER_SECRET``).
        """
        self._database = database
        self.store = store
        self._signer = SessionSigner(secret)
        self._failures: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- plumbing

    def build(self) -> Starlette:
        """The Starlette application."""
        routes = [
            Route("/style.css", self._style, methods=["GET"]),
            self._route("/", self.home, ["GET"]),
            self._route("/login", self.login_form, ["GET"]),
            self._route("/login", self.login, ["POST"]),
            self._route("/logout", self.logout, ["POST"]),
            self._route("/project/{project_id:int}", self.project, ["GET"]),
            self._route("/chat/{chat_id}", self.chat, ["GET"]),
            self._route("/chat/{chat_id}/report.md", self.report, ["GET"]),
            self._route("/chat/{chat_id}/report.html", self.report_html, ["GET"]),
            self._route(
                "/chat/{chat_id}/decisions/{decision_id:int}/review", self.review, ["POST"]
            ),
            self._route("/approaches", library_views.approaches, ["GET"]),
            self._route("/approaches/compare", library_views.compare, ["GET"]),
            self._route("/approach/{decision_id:int}", library_views.approach, ["GET"]),
            self._route("/timeline", library_views.timeline, ["GET"]),
            self._route("/review", library_views.review_queue, ["GET"]),
            self._route("/activity", library_views.activity, ["GET"]),
            self._route("/insights", library_views.insights, ["GET"]),
            self._route("/alerts", library_views.alerts, ["GET"]),
            self._route("/access-log", self.access_log, ["GET"]),
            self._route("/admin/users", self.admin_users, ["GET"]),
            self._route("/admin/users", self.create_user, ["POST"]),
            self._route("/admin/users/{user_id:int}/role", self.change_role, ["POST"]),
            self._route("/admin/users/{user_id:int}/account", self.change_account, ["POST"]),
            self._route("/admin/users/{user_id:int}/disable", self.change_disabled, ["POST"]),
            self._route("/admin/users/{user_id:int}/password", self.change_password, ["POST"]),
            self._route("/admin/labels", self.admin_labels, ["GET"]),
            self._route("/admin/labels", self.save_label, ["POST"]),
        ]
        return Starlette(routes=routes)

    def _route(self, path: str, handler: Handler, methods: list[str]) -> Route:
        """A route that runs ``handler`` in a worker thread with a connection and a session."""

        async def endpoint(request: Request) -> Response:
            form = await _read_form(request) if request.method == "POST" else {}
            if form is None:
                return HTMLResponse("The form is too large.", status_code=413)
            return await run_in_threadpool(self._dispatch, handler, request, form)

        return Route(path, endpoint, methods=methods)

    def _dispatch(self, handler: Handler, request: Request, form: dict[str, str]) -> Response:
        """Open the connection, resolve the visitor, check the CSRF token and call the handler."""
        base = str(request.scope.get("root_path", "")).rstrip("/")
        try:
            connection = connect(self._database)
        except Exception as error:  # noqa: BLE001  (any failure to connect means "unavailable")
            logger.warning("database unavailable: %s", type(error).__name__)
            return _with_headers(
                HTMLResponse("The database is not reachable. Try again later.", status_code=503)
            )
        try:
            session, user = self._visitor(connection, request)
            csrf = self._signer.csrf_token(session) if session else ""
            ctx = Context(request, form, connection, base, session, user, csrf)
            if request.method == "POST" and not self._signer.csrf_ok(session, form.get("csrf", "")):
                response = ctx.page(
                    pages.message_page(ctx.view, "Form expired", "Reload the page and try again."),
                    403,
                )
            else:
                response = handler(ctx)
            connection.commit()
        except Exception as error:  # noqa: BLE001  (a handler bug must not leak details)
            connection.rollback()
            logger.error("explorer error: %s", type(error).__name__, exc_info=True)
            return _with_headers(HTMLResponse("Something went wrong.", status_code=500))
        finally:
            connection.close()
        self._apply_cookie(request, response, ctx)
        return _with_headers(response)

    def _visitor(
        self, connection: pg8000.dbapi.Connection, request: Request
    ) -> tuple[SessionData | None, UiUser | None]:
        """The session in the cookie and its user; a stale one counts as logged out.

        Stale means the user is gone or disabled, or the password changed since login.
        """
        session = self._signer.unsign(request.cookies.get(COOKIE))
        if session is None or session.user_id is None:
            return session, None
        user = users.find_user(connection, session.user_id)
        stale = (
            user is None
            or user.disabled
            or session.password_mark != self._signer.password_mark(user.password_hash)
        )
        return (None, None) if stale else (session, user)

    def _apply_cookie(self, request: Request, response: Response, ctx: Context) -> None:
        """Set or clear the session cookie as the handler asked."""
        secure = (
            request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        )
        path = ctx.base or "/"
        if ctx.clear_session:
            response.delete_cookie(COOKIE, path=path)
        elif ctx.new_session is not None:
            response.set_cookie(
                COOKIE,
                self._signer.sign(ctx.new_session),
                max_age=SESSION_SECONDS,
                path=path,
                httponly=True,
                secure=secure,
                samesite="lax",
            )

    # ---------------------------------------------------------------- login

    async def _style(self, request: Request) -> Response:
        """The style sheet."""
        return Response(
            pages.CSS, media_type="text/css", headers={**_HEADERS, "Cache-Control": "max-age=3600"}
        )

    def login_form(self, ctx: Context) -> Response:
        """The login form (starts an anonymous session so that the form has a CSRF token)."""
        if ctx.user is not None:
            return ctx.redirect("/")
        session = ctx.session or self._signer.new_session(None, "", SESSION_SECONDS)
        ctx.new_session = session
        ctx.csrf = self._signer.csrf_token(session)
        return ctx.page(pages.login_page(ctx.view))

    def login(self, ctx: Context) -> Response:
        """Check the password; on success start a new session (a new id every login)."""
        email = ctx.form.get("email", "").strip().lower()
        if self._too_many_failures(email):
            return ctx.page(
                pages.login_page(ctx.view, "Too many failed attempts. Wait a few minutes."), 429
            )
        user = users.find_user_by_email(ctx.connection, email) if email else None
        good = verify_password(ctx.form.get("password", ""), user.password_hash if user else None)
        if user is None or user.disabled or not good:
            self._failures.setdefault(email, []).append(time.monotonic())
            return ctx.page(pages.login_page(ctx.view, "Wrong e-mail address or password."), 401)
        self._failures.pop(email, None)
        session = self._signer.new_session(
            user.user_id, self._signer.password_mark(user.password_hash), SESSION_SECONDS
        )
        ctx.new_session = session
        users.record_access(ctx.connection, user.user_id, "login", None)
        return ctx.redirect("/")

    def logout(self, ctx: Context) -> Response:
        """End the session."""
        if ctx.user is not None:
            users.record_access(ctx.connection, ctx.user.user_id, "logout", None)
        ctx.clear_session = True
        return ctx.redirect("/login")

    def _too_many_failures(self, email: str) -> bool:
        """Whether this address has failed to log in too often lately."""
        recent = [
            t for t in self._failures.get(email, []) if time.monotonic() - t < LOGIN_WINDOW_SECONDS
        ]
        self._failures[email] = recent
        return len(recent) >= LOGIN_ATTEMPTS

    # ---------------------------------------------------------------- browsing

    def home(self, ctx: Context) -> Response:
        """The tree of Claude accounts and projects."""
        if ctx.user is None:
            return ctx.redirect("/login")
        return ctx.page(pages.home_page(ctx.view, queries.home_tree(ctx.connection)))

    def project(self, ctx: Context) -> Response:
        """The chats of one project (id 0: chats without a project)."""
        if ctx.user is None:
            return ctx.redirect("/login")
        project_id = int(ctx.request.path_params["project_id"])
        project = queries.project_name(ctx.connection, project_id) if project_id else None
        if project_id and project is None:
            return self._not_found(ctx)
        account = ctx.query("account")
        filter_account = account is not None
        account_id = int(account) if account and account.isdigit() else None
        chats = queries.chat_list(
            ctx.connection,
            project_id=project_id or None,
            account_id=account_id,
            filter_account=filter_account,
            show_empty=ctx.query("show_empty") == "1",
        )
        text = pages.project_page(
            ctx.view, project, account, chats, ctx.query("show_empty") == "1", ctx.user.account_id
        )
        return ctx.page(text)

    def chat(self, ctx: Context) -> Response:
        """One chat, one tab. 404 for a chat that does not exist or that this user may not open."""
        if ctx.user is None:
            return ctx.redirect("/login")
        chat_id = str(ctx.request.path_params["chat_id"])
        chat = queries.chat_summary(ctx.connection, chat_id)
        if chat is None or not ctx.user.may_open_chat(chat["account_id"]):
            return self._not_found(ctx)
        users.record_access(ctx.connection, ctx.user.user_id, "view_chat", chat_id)
        tab = ctx.query("tab") or "conversation"
        if tab not in dict(pages.TABS):
            tab = "conversation"
        body = self._tab_body(ctx, chat_id, tab)
        flash = "Decision updated." if ctx.query("done") == "review" else ""
        return ctx.page(pages.chat_page(ctx.view, chat, tab, body, flash))

    def report(self, ctx: Context) -> Response:
        """The whole chat as one Markdown file (download). Same access rule as the chat page."""
        if ctx.user is None:
            return ctx.redirect("/login")
        chat_id = str(ctx.request.path_params["chat_id"])
        chat = queries.chat_summary(ctx.connection, chat_id)
        if chat is None or not ctx.user.may_open_chat(chat["account_id"]):
            return self._not_found(ctx)
        users.record_access(ctx.connection, ctx.user.user_id, "export_chat", chat_id)
        text = report_of_chat(ctx.connection, self.store, chat_id)
        return _with_headers(
            Response(
                text,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="chat-{chat_id[:8]}.md"'},
            )
        )

    def report_html(self, ctx: Context) -> Response:
        """The whole chat as a styled page (for showing to a client). Same access rule."""
        if ctx.user is None:
            return ctx.redirect("/login")
        chat_id = str(ctx.request.path_params["chat_id"])
        chat = queries.chat_summary(ctx.connection, chat_id)
        if chat is None or not ctx.user.may_open_chat(chat["account_id"]):
            return self._not_found(ctx)
        users.record_access(ctx.connection, ctx.user.user_id, "view_report", chat_id)
        page = report_page(chat.get("title"), report_of_chat(ctx.connection, self.store, chat_id))
        # The page loads its Markdown renderer from cdn.jsdelivr.net, so it gets its own policy.
        return HTMLResponse(
            page,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                    "style-src 'unsafe-inline'; img-src data: https:; frame-ancestors 'none'"
                )
            },
        )

    def _tab_body(self, ctx: Context, chat_id: str, tab: str) -> str:
        """The HTML of one tab of a chat."""
        connection = ctx.connection
        user = ctx.user
        if user is None:
            raise PermissionError("this page needs a logged-in user")
        if tab == "conversation":
            page = max(
                int(ctx.query("page") or 1) if (ctx.query("page") or "1").isdigit() else 1, 1
            )
            events, total = queries.conversation(connection, chat_id, page)
            return pages.conversation_tab(ctx.view, chat_id, events, total, page)
        if tab == "sessions":
            return pages.sessions_tab(queries.sessions(connection, chat_id))
        if tab == "decisions":
            rows = queries.decisions(connection, chat_id)
            return pages.decisions_tab(ctx.view, chat_id, rows, user.may_review)
        if tab == "tests":
            return pages.tests_tab(*queries.test_runs(connection, chat_id))
        if tab == "code":
            return pages.code_tab(*queries.file_changes(connection, chat_id))
        if tab == "changes":
            return library_views.changes_tab(ctx, chat_id)
        if tab == "corrections":
            return pages.corrections_tab(queries.corrections(connection, chat_id))
        if tab == "agents":
            return pages.agents_tab(*queries.agents(connection, chat_id))
        if tab == "thinking":
            return pages.thinking_tab(queries.thinking(connection, chat_id))
        return pages.usage_tab(queries.usage(connection, chat_id))

    def review(self, ctx: Context) -> Response:
        """A lead, auditor or admin approves or rejects one decision, with a note."""
        if ctx.user is None:
            return ctx.redirect("/login")
        chat_id = str(ctx.request.path_params["chat_id"])
        chat = queries.chat_summary(ctx.connection, chat_id)
        if chat is None or not ctx.user.may_open_chat(chat["account_id"]):
            return self._not_found(ctx)
        if not ctx.user.may_review:
            return self._forbidden(ctx)
        status = {"approve": "approved", "reject": "rejected"}.get(ctx.form.get("status", ""))
        note = ctx.form.get("note", "").strip()
        if status is None or len(note) > MAX_NOTE_CHARS:
            return ctx.page(
                pages.message_page(
                    ctx.view,
                    "Not saved",
                    f"Choose approve or reject; a note has at most {MAX_NOTE_CHARS} characters.",
                ),
                400,
            )
        decision_id = int(ctx.request.path_params["decision_id"])
        if not queries.review_decision(
            ctx.connection, chat_id, decision_id, status, ctx.user.reviewer_name, note
        ):
            return self._not_found(ctx)
        users.record_access(
            ctx.connection, ctx.user.user_id, "review_decision", f"{chat_id}#{decision_id}:{status}"
        )
        # The chat's catalog files and report.md show who reviewed each decision: have the server
        # write them again (an index job; its after-index step rebuilds the folder).
        ctx.connection.cursor().execute(
            "INSERT INTO job (job_type, session_id) VALUES ('index_session', %s) "
            "ON CONFLICT (session_id) WHERE job_type = 'index_session' AND status = 'queued' "
            "DO NOTHING",
            (chat_id,),
        )
        return ctx.redirect(f"/chat/{quote(chat_id)}", tab="decisions", done="review")

    def access_log(self, ctx: Context) -> Response:
        """Who opened what (admins and auditors)."""
        if ctx.user is None:
            return ctx.redirect("/login")
        if not ctx.user.reads_access_log:
            return self._forbidden(ctx)
        before = ctx.query("before")
        limit = 200
        rows = queries.access_log(
            ctx.connection, int(before) if before and before.isdigit() else None, limit + 1
        )
        next_before = rows[limit - 1]["log_id"] if len(rows) > limit else None
        return ctx.page(pages.access_log_page(ctx.view, rows[:limit], next_before))

    # ---------------------------------------------------------------- administration

    def admin_users(self, ctx: Context) -> Response:
        """The user list and the new-user form."""
        denied = self._admin_only(ctx)
        if denied is not None:
            return denied
        return ctx.page(self._users_page(ctx, _DONE.get(ctx.query("done") or "", "")))

    def _users_page(self, ctx: Context, flash: str = "", error: bool = False) -> str:
        return pages.users_page(
            ctx.view,
            users.list_users(ctx.connection),
            queries.accounts(ctx.connection),
            flash,
            error,
        )

    def create_user(self, ctx: Context) -> Response:
        """Add a user."""
        denied = self._admin_only(ctx)
        if denied is not None:
            return denied
        account = ctx.form.get("account_id", "")
        try:
            users.create_user(
                ctx.connection,
                ctx.form.get("email", ""),
                ctx.form.get("role", ""),
                ctx.form.get("password", ""),
                full_name=ctx.form.get("full_name", ""),
                account_id=int(account) if account.isdigit() else None,
            )
        except (UserError, PasswordError) as error:
            ctx.connection.rollback()
            return ctx.page(self._users_page(ctx, str(error), True), 400)
        return ctx.redirect("/admin/users", done="created")

    def change_role(self, ctx: Context) -> Response:
        """Change a user's role (not your own: an admin could lock everyone out)."""
        return self._change_user(
            ctx,
            "role",
            lambda uid: users.set_role(ctx.connection, uid, ctx.form.get("role", "")),
            own_forbidden=True,
        )

    def change_account(self, ctx: Context) -> Response:
        """Link a user to a Claude account, or remove the link."""
        account = ctx.form.get("account_id", "")
        return self._change_user(
            ctx, "account",
            lambda uid: users.link_account(
                ctx.connection, uid, int(account) if account.isdigit() else None
            ),
        )  # fmt: skip

    def change_disabled(self, ctx: Context) -> Response:
        """Disable or enable a user (not yourself)."""
        disable = ctx.form.get("disabled") == "1"
        return self._change_user(
            ctx, "disabled" if disable else "enabled",
            lambda uid: users.set_disabled(ctx.connection, uid, disable), own_forbidden=True,
        )  # fmt: skip

    def change_password(self, ctx: Context) -> Response:
        """Give a user a new password."""
        return self._change_user(
            ctx,
            "password",
            lambda uid: users.reset_password(ctx.connection, uid, ctx.form.get("password", "")),
        )

    def _change_user(
        self, ctx: Context, done: str, change: Callable[[int], None], own_forbidden: bool = False
    ) -> Response:
        """Run one change of one user and show the user list again."""
        denied = self._admin_only(ctx)
        if denied is not None:
            return denied
        user_id = int(ctx.request.path_params["user_id"])
        if own_forbidden and ctx.user is not None and user_id == ctx.user.user_id:
            return ctx.page(
                self._users_page(ctx, "You cannot do that to your own user.", True), 400
            )
        try:
            change(user_id)
        except (UserError, PasswordError) as error:
            ctx.connection.rollback()
            return ctx.page(self._users_page(ctx, str(error), True), 400)
        return ctx.redirect("/admin/users", done=done)

    def admin_labels(self, ctx: Context) -> Response:
        """The label lists of accounts and projects."""
        denied = self._admin_only(ctx)
        if denied is not None:
            return denied
        return ctx.page(self._labels_page(ctx, _DONE.get(ctx.query("done") or "", "")))

    def _labels_page(self, ctx: Context, flash: str = "", error: bool = False) -> str:
        return pages.labels_page(
            ctx.view,
            queries.accounts(ctx.connection),
            queries.projects(ctx.connection),
            flash,
            error,
        )

    def save_label(self, ctx: Context) -> Response:
        """Set a label, or remove it when the field is empty."""
        denied = self._admin_only(ctx)
        if denied is not None:
            return denied
        kind, key, label = (
            ctx.form.get("kind", ""),
            ctx.form.get("key", ""),
            ctx.form.get("label", "").strip(),
        )
        try:
            if not label:
                remove_label(ctx.connection, kind, key)
            elif kind == "account":
                set_account_label(ctx.connection, key, label)
            elif kind == "project":
                set_project_label(ctx.connection, key, label)
            else:
                raise LabelError("The kind must be 'account' or 'project'")
        except LabelError as error:
            ctx.connection.rollback()
            return ctx.page(self._labels_page(ctx, str(error), True), 400)
        return ctx.redirect("/admin/labels", done="label" if label else "unlabel")

    # ---------------------------------------------------------------- refusals

    def _admin_only(self, ctx: Context) -> Response | None:
        """A redirect to the login page, a 403 page, or ``None`` when the user is an admin."""
        if ctx.user is None:
            return ctx.redirect("/login")
        return None if ctx.user.is_admin else self._forbidden(ctx)

    def _forbidden(self, ctx: Context) -> Response:
        return ctx.page(
            pages.message_page(ctx.view, "Not allowed", "Your role may not do that."), 403
        )

    def _not_found(self, ctx: Context) -> Response:
        return ctx.page(pages.message_page(ctx.view, "Not found", "There is nothing here."), 404)


async def _read_form(request: Request) -> dict[str, str] | None:
    """The fields of an urlencoded form (first value of each), or ``None`` if it is too large."""
    if int(request.headers.get("content-length", "0") or 0) > MAX_FORM_BYTES:
        return None
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return None
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


def _with_headers(response: Response) -> Response:
    """Add the security headers to a response."""
    for name, value in _HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def create_app(
    database_settings: DatabaseSettings, store: ObjectStore | None = None, *, secret: str
) -> Starlette:
    """Build the explorer website.

    Args:
        database_settings: The database holding the index (migration 016 must be applied).
        store: The raw object store (reserved; the pages need only the database).
        secret: Signs session cookies and form tokens; at least 16 characters.

    Returns:
        A Starlette application. Mount it at ``/explorer`` or serve it on its own.

    Raises:
        ValueError: If the secret is too short.
    """
    return ExplorerApp(database_settings, store, secret).build()
