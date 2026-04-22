"""
User authentication module using Supabase Auth.
Provides sign-up, sign-in, sign-out, and session management helpers.
"""

from __future__ import annotations

from typing import Optional
import streamlit as st
from supabase import Client

import config
import db


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
    """Sign out the current user and clear Streamlit session state."""
    if "supabase_session" in st.session_state:
        try:
            client = get_supabase_client()
            client.auth.sign_out()
        except Exception:
            pass
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


def _try_refresh_session() -> bool:
    """Attempt to refresh the Supabase session using the stored refresh token."""
    session = st.session_state.get("supabase_session")
    if not session or not hasattr(session, "refresh_token") or not session.refresh_token:
        return False
    try:
        client = get_supabase_client()
        res = client.auth.refresh_session(session.refresh_token)
        if res.session and res.user:
            _store_session({"session": res.session, "user": res.user})
            return True
    except Exception:
        pass
    return False


def require_auth() -> tuple[Client, dict]:
    """
    Enforce authentication. If the user is not logged in, show the login
    UI and stop page rendering via st.stop().

    Returns (authenticated_client, user_dict).
    """
    if "current_user" not in st.session_state:
        _render_login_page()
        st.stop()
    client = get_authenticated_client()
    if client is None:
        # Try refreshing the token before forcing re-login
        if _try_refresh_session():
            client = get_authenticated_client()
        if client is None:
            sign_out()
            _render_login_page()
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


def _render_login_page() -> None:
    """Render the login / registration form."""
    st.markdown(
        f"""
        <div class='login-hero'>
          <div class='lh-mark'>✦</div>
          <div class='lh-badge'>Welcome</div>
          <div class='lh-title'>{config.APP_TITLE}</div>
          <div class='lh-sub'>AutoWriter · v{config.APP_VERSION}</div>
        </div>
        """,
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
                _store_session(result)
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
                    _store_session(result)
                    st.success("注册成功！")
                    st.rerun()
            except Exception as exc:
                st.error(f"注册失败：{_friendly_auth_error(exc)}")


def _store_session(result: dict) -> None:
    """Persist auth result into Streamlit session state."""
    session = result.get("session")
    user = result.get("user")
    if session:
        st.session_state["access_token"] = session.access_token
        st.session_state["supabase_session"] = session
    if user:
        st.session_state["current_user"] = {
            "id": user.id,
            "email": user.email,
        }
