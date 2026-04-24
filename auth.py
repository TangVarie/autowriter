"""
User authentication module using Supabase Auth.
Provides sign-up, sign-in, sign-out, and session management helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import streamlit as st
from supabase import Client

import config
import db

try:
    import extra_streamlit_components as stx
    _COOKIES_AVAILABLE = True
except ImportError:
    _COOKIES_AVAILABLE = False


# ── Cookie-backed session persistence ─────────────────────────────────────
# Streamlit session_state lives in the websocket connection; F5 resets it.
# We persist the Supabase refresh token in a browser cookie so that after a
# refresh we can re-derive an access token without making the user re-login.
#
# Cookie-manager lifecycle note: stx.CookieManager must be instantiated
# EXACTLY ONCE per Streamlit rerun (duplicate-key error otherwise) AND its
# internal cookies dict is populated asynchronously via the iframe response.
# We therefore instantiate it at the top of ``require_auth`` and thread the
# instance through every helper that needs it, rather than caching across
# reruns (a cached CM has a stale cookies dict and never sees fresh values).
_COOKIE_NAME = "xhs_rt"
_COOKIE_TTL_DAYS = 7


def _make_cookie_manager():
    """Instantiate a fresh CookieManager for this rerun, or None if the
    library isn't installed.  Do not call more than once per rerun — the
    stx library keys its internal component by the key arg and raises
    a Streamlit duplicate-key error on the second call."""
    if not _COOKIES_AVAILABLE:
        return None
    return stx.CookieManager(key="xhs_auth_cm")


# Sentinel returned by ``_load_cookies_once`` when the iframe hasn't posted
# back yet — callers treat this as "wait, don't show login".
_COOKIES_LOADING = object()


def _load_cookies_once(cm):
    """Call ``cm.get_all()`` **exactly once** per rerun and return the cached
    result.

    stx's ``get_all`` / ``get`` / ``set`` / ``delete`` each register a
    component with Streamlit; calling ``get_all`` twice in one rerun raises
    StreamlitDuplicateElementKey.  We stash the dict on the cm object itself
    (``_xhs_cookies_cache``) so helpers further down the request can reuse it
    without re-triggering the component."""
    if cm is None:
        return {}
    cached = getattr(cm, "_xhs_cookies_cache", None)
    if cached is not None:
        return cached
    try:
        cookies = cm.get_all()
    except Exception:
        cookies = {}
    if cookies is None:
        return _COOKIES_LOADING
    setattr(cm, "_xhs_cookies_cache", cookies)
    return cookies


def _persist_refresh_token(cm, refresh_token: str) -> None:
    """Write the refresh token cookie.  ``cm.set()`` is asynchronous: it posts
    to an iframe which then calls ``document.cookie = ...``; if the Streamlit
    script reruns before the iframe finishes, the write is aborted and the
    cookie is silently dropped.  Callers should pair this with
    :func:`wait_for_cookie_write` before any ``st.rerun()``."""
    if cm is None or not refresh_token:
        return
    try:
        cm.set(
            _COOKIE_NAME,
            refresh_token,
            expires_at=datetime.now(timezone.utc) + timedelta(days=_COOKIE_TTL_DAYS),
            key="xhs_set_rt",
        )
    except Exception:
        pass


def wait_for_cookie_write(seconds: float = 0.6) -> None:
    """Block briefly so the stx iframe's JS can finish writing the cookie
    before the Streamlit script reruns.  600 ms is long enough on typical
    connections without being noticeable to the user."""
    import time
    time.sleep(max(0.1, seconds))


def _clear_refresh_cookie(cm) -> None:
    if cm is None:
        return
    try:
        cm.delete(_COOKIE_NAME, key="xhs_del_rt")
    except Exception:
        pass


def get_supabase_client() -> Client:
    """Return an anonymous (unauthenticated) Supabase client."""
    return db.get_client()


def sign_up(email: str, password: str) -> dict:
    """
    Register a new user.
    Returns {"user": ..., "session": ..., "email_confirmation_required": bool}.
    Raises on hard errors (e.g. email already registered, rate limit).
    """
    client = get_supabase_client()
    res = client.auth.sign_up({"email": email, "password": password})
    if res.user is None:
        raise ValueError("注册失败，请稍后重试。")
    email_confirmation_required = res.session is None
    return {
        "user": res.user,
        "session": res.session,
        "email_confirmation_required": email_confirmation_required,
    }


def sign_in(email: str, password: str) -> dict:
    """Sign in with email/password. Raises on error."""
    client = get_supabase_client()
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    if res.user is None:
        raise ValueError("邮箱或密码不正确。")
    return {"user": res.user, "session": res.session}


def sign_out() -> None:
    """
    Sign out the current user and clear Streamlit session state + cookie.

    Called from the sidebar logout button (a separate rerun), so a fresh
    CookieManager is safe to instantiate locally here.  If the lib isn't
    available the cookie delete is a no-op.
    """
    if "supabase_session" in st.session_state:
        try:
            client = get_supabase_client()
            client.auth.sign_out()
        except Exception:
            pass
    _clear_refresh_cookie(_make_cookie_manager())
    for key in ("supabase_session", "current_user", "access_token", "current_project_id"):
        st.session_state.pop(key, None)


def get_authenticated_client() -> Optional[Client]:
    """
    Return a Supabase client authenticated with the current user's JWT.
    Returns None if the user is not logged in.
    """
    token = st.session_state.get("access_token")
    if not token:
        return None
    return db.get_client(access_token=token)


def _try_refresh_session(cm) -> bool:
    """Attempt to refresh the Supabase session using the stored refresh token."""
    session = st.session_state.get("supabase_session")
    if not session or not hasattr(session, "refresh_token") or not session.refresh_token:
        return False
    try:
        client = get_supabase_client()
        res = client.auth.refresh_session(session.refresh_token)
        if res.session and res.user:
            _store_session({"session": res.session, "user": res.user}, cm=cm)
            return True
    except Exception:
        pass
    return False


def _try_cookie_restore(cm, refresh_token: Optional[str]) -> bool:
    """Re-hydrate the session from a refresh-token string already read out of
    the cookies dict.  Returns True iff a session was successfully restored."""
    if not refresh_token:
        return False
    try:
        client = get_supabase_client()
        res = client.auth.refresh_session(refresh_token)
        if res.session and res.user:
            _store_session({"session": res.session, "user": res.user}, cm=cm)
            return True
    except Exception:
        pass
    _clear_refresh_cookie(cm)
    return False


def require_auth() -> tuple[Client, dict]:
    """
    Enforce authentication. If the user is not logged in, show the login
    UI and stop page rendering via st.stop().

    On first request after a browser refresh, session_state is empty; we
    try to restore from the refresh-token cookie before falling back to
    login.  A fresh CookieManager is instantiated here — exactly once per
    rerun — and threaded through every helper that needs cookie access.

    Returns (authenticated_client, user_dict).
    """
    cm = _make_cookie_manager()
    cookies = _load_cookies_once(cm)

    if "current_user" not in st.session_state:
        if cookies is _COOKIES_LOADING:
            # stx's iframe hasn't posted back yet; Streamlit will rerun after
            # the component responds.  Show a placeholder instead of flashing
            # the login page — otherwise every refresh looks like a logout.
            st.caption("正在恢复登录状态…")
            st.stop()
        refresh_token = cookies.get(_COOKIE_NAME) if isinstance(cookies, dict) else None
        if not _try_cookie_restore(cm, refresh_token):
            _render_login_page(cm, cookies)
            st.stop()

    client = get_authenticated_client()
    if client is None:
        # Access token likely expired — try refreshing before kicking the user out.
        if _try_refresh_session(cm):
            client = get_authenticated_client()
        if client is None:
            sign_out()
            _render_login_page(cm, cookies)
            st.stop()
    return client, st.session_state["current_user"]


def _friendly_auth_error(exc: Exception) -> str:
    """Convert a Supabase auth exception into a human-readable Chinese message."""
    msg = str(exc).lower()
    if "email not confirmed" in msg or "email_not_confirmed" in msg:
        return (
            "邮箱尚未验证。请检查您的收件箱（含垃圾邮件），点击确认链接后再登录。\n\n"
            "如果一直未收到邮件，请联系管理员在 Supabase 后台手动确认账号。"
        )
    if "invalid login credentials" in msg or "invalid email or password" in msg:
        return "邮箱或密码不正确，请重试。"
    if "user already registered" in msg or "already been registered" in msg:
        return "该邮箱已注册，请直接登录，或使用忘记密码功能。"
    if "email rate limit" in msg or "rate limit" in msg or "over_email_send_rate_limit" in msg:
        return (
            "系统邮件发送已达上限（Supabase 免费额度限制）。\n\n"
            "请联系管理员在 Supabase 控制台手动确认该账号，或稍等一段时间再试。"
        )
    if "password" in msg and ("weak" in msg or "short" in msg or "length" in msg):
        return "密码强度不足，请使用至少 6 位包含字母和数字的密码。"
    # fallback: return raw message
    return str(exc)


def _auth_debug_lines(cm, cookies) -> list[str]:
    """Return a bullet list describing the current state of the cookie-based
    session restore pipeline.  ``cookies`` is the already-loaded dict from
    ``_load_cookies_once`` (never call ``cm.get_all`` again here — we'd hit
    stx's duplicate-key guard)."""
    lines: list[str] = []
    lines.append(f"• cookie 库：{'已安装' if _COOKIES_AVAILABLE else '未安装（刷新就会掉登录）'}")
    if not _COOKIES_AVAILABLE:
        lines.append("  → 请确认 requirements.txt 里有 extra-streamlit-components，并在 Streamlit Cloud 上重新部署")
        return lines
    if cm is None:
        lines.append("• cm 实例：为 None（异常）")
        return lines
    if cookies is _COOKIES_LOADING:
        lines.append("• cookies 状态：iframe 还没回传（正常情况下会自动再 rerun 一次）")
        return lines
    if not isinstance(cookies, dict) or not cookies:
        lines.append("• cookies 状态：空（浏览器里没存过 refresh token，或首次登录）")
        return lines
    lines.append(f"• cookies 状态：已就绪，共 {len(cookies)} 个")
    rt = cookies.get(_COOKIE_NAME)
    if not rt:
        lines.append(f"• {_COOKIE_NAME}：未找到（上次登录写 cookie 失败了？）")
    else:
        lines.append(f"• {_COOKIE_NAME}：存在（首 8 字：{str(rt)[:8]}…）")
        lines.append("  → refresh_session 尝试失败。可能是 token 已被 Supabase 撤销")
    return lines


def _render_login_page(cm, cookies=None) -> None:
    """Render the login / registration form — studio two-column landing."""
    # Trim page top padding for the login screen only
    st.markdown(
        "<style>.main .block-container{padding-top:1.25rem !important}</style>",
        unsafe_allow_html=True,
    )

    col_hero, col_frame = st.columns([1.1, 1], gap="large")

    with col_hero:
        st.markdown(
            f"""
            <div class='login-hero'>
              <div class='lh-mark'>✦</div>
              <div class='lh-badge'>▸ Workstation</div>
              <div class='lh-title'>{config.APP_TITLE}</div>
              <div class='lh-sub'>
                一个为小红书内容创作者打造的自动化工作台。
                生成、审核、导出、沉淀风格记忆,一气呵成。
              </div>
              <div class='lh-meta'>
                <span><b>VERSION</b> {config.APP_VERSION}</span>
                <span><b>ENGINES</b> Claude · Gemini</span>
                <span><b>STATUS</b> ● READY</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_frame:
        st.markdown(
            "<div class='section-label' style='margin-top:0'>Access</div>",
            unsafe_allow_html=True,
        )

        missing = config.validate_config()
        if missing:
            st.error(
                f"⚠️ Missing environment variables: {', '.join(missing)}\n\n"
                "Please configure them before using the app."
            )
            return

        tab_login, tab_signup = st.tabs(["登录", "注册"])

        with tab_login:
            with st.form("login_form"):
                email = st.text_input("邮箱")
                password = st.text_input("密码", type="password")
                submitted = st.form_submit_button("登录", use_container_width=True)
            if submitted:
                if not email or not password:
                    st.error("请填写邮箱和密码。")
                    return
                try:
                    result = sign_in(email, password)
                    _store_session(result, cm=cm)
                    wait_for_cookie_write()
                    st.rerun()
                except Exception as exc:
                    st.error(_friendly_auth_error(exc))

        with tab_signup:
            with st.form("signup_form"):
                new_email    = st.text_input("邮箱", key="signup_email")
                new_password = st.text_input("密码（至少 6 位）", type="password", key="signup_pw")
                new_password2 = st.text_input("确认密码", type="password", key="signup_pw2")
                submitted2 = st.form_submit_button("注册", use_container_width=True)

            if submitted2:
                if not new_email or not new_password:
                    st.error("请填写邮箱和密码。")
                    return
                if new_password != new_password2:
                    st.error("两次密码不一致。")
                    return
                try:
                    result = sign_up(new_email, new_password)
                    if result.get("email_confirmation_required"):
                        # Supabase email confirmation is enabled — do NOT start a session yet.
                        st.success(
                            "注册申请已提交！\n\n"
                            "请检查您的收件箱（**含垃圾邮件夹**），点击确认链接完成验证后即可登录。\n\n"
                            "如果长时间未收到邮件，请联系管理员手动激活账号。"
                        )
                    else:
                        # Email confirmation is disabled — session is immediately available.
                        _store_session(result, cm=cm)
                        wait_for_cookie_write()
                        st.success("注册成功！")
                        st.rerun()
                except Exception as exc:
                    st.error(f"注册失败：{_friendly_auth_error(exc)}")

    # Diagnostic panel — why did we end up on the login page?  Visible under
    # an expander so it doesn't clutter the normal first-login experience.
    with st.expander("🔧 登录持久化诊断（刷新后掉登录？展开看原因）", expanded=False):
        for line in _auth_debug_lines(cm, cookies):
            st.caption(line)


def _store_session(result: dict, cm=None) -> None:
    """Persist auth result into Streamlit session state and the cookie jar.

    ``cm`` is the rerun-local CookieManager from ``require_auth``; if the
    caller doesn't pass one (e.g. legacy flows) we just skip the cookie
    write rather than instantiate a second CM in the same rerun.
    """
    session = result.get("session")
    user = result.get("user")
    if session:
        st.session_state["access_token"] = session.access_token
        st.session_state["supabase_session"] = session
        # Rotate the cookie on every session write so newly-issued refresh
        # tokens replace the old one (Supabase revokes refresh tokens after
        # use in rotation mode).
        rt = getattr(session, "refresh_token", None)
        if rt and cm is not None:
            _persist_refresh_token(cm, rt)
    if user:
        st.session_state["current_user"] = {
            "id": user.id,
            "email": user.email,
        }
