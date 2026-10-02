"""Launch with: python -m streamlit run streamlit_app.py"""

import streamlit as st

from support_ai.web_client import APIError, request


st.set_page_config(page_title="Northstar Support", page_icon="💬", layout="centered")
st.title("Northstar Support")
st.caption("Ask about customers, orders, support tickets, and company policies.")

if "messages" not in st.session_state:
    st.session_state.messages = []


def credentials():
    if "credentials" not in st.session_state:
        st.session_state.credentials = request("POST", "/sessions")
    return st.session_state.credentials


def show_error(exc):
    st.error(str(exc))
    if exc.status == 401:
        st.session_state.pop("credentials", None)
        st.session_state.session_expired = True
        st.info("Start a new conversation to continue. Previous messages below are for reference only.")


with st.sidebar:
    st.header("Conversation")
    if st.button("New conversation", use_container_width=True):
        try:
            old = st.session_state.get("credentials")
            if old:
                try:
                    request("DELETE", f"/sessions/{old['session_id']}", credentials=old)
                except APIError as exc:
                    if exc.status != 401:
                        raise
            st.session_state.pop("credentials", None)
            st.session_state.messages = []
            st.session_state.session_expired = False
            st.rerun()
        except APIError as exc:
            show_error(exc)
    st.divider()
    st.header("Company policies")
    st.caption("Upload a text-based PDF up to 20 MiB. Policies are shared across conversations.")
    uploaded = st.file_uploader("Policy PDF", type=["pdf"], key="policy_file")
    if st.button("Add policy", disabled=uploaded is None, use_container_width=True):
        if uploaded.size > 20 * 1024 * 1024:
            st.error("PDF must be no larger than 20 MiB.")
        else:
            try:
                with st.spinner("Reading and indexing the policy…"):
                    auth = credentials()
                    result = request("POST", "/policies", credentials=auth,
                        params={"session_id": auth["session_id"]},
                        files={"file": (uploaded.name, uploaded.getvalue(), "application/pdf")})
                if result["status"] == "duplicate":
                    st.info("This policy is already indexed.")
                else:
                    st.success(f"Added {result['source']}. You can now ask about it.")
            except APIError as exc:
                show_error(exc)
    st.divider()
    st.caption("Local assessment demo using fictional customer records. Chat and indexing use OpenAI.")

if st.session_state.get("session_expired", False):
    st.warning("This conversation expired. Select New conversation to continue.")

if not st.session_state.messages:
    st.markdown("**Try a question**")
    st.markdown('- "Show Emma’s open support tickets."\n- "What is the refund policy?"\n- "Can Emma Wilson return her latest order?"')

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message.get("status") == "error":
            st.error(message["content"])
        else:
            st.markdown(message["content"])
        if message.get("sources"):
            with st.expander("Policy sources"):
                for source in {item["citation"] for item in message["sources"]}:
                    st.write(source)

if prompt := st.chat_input("Ask a support question", max_chars=4000,
                           disabled=st.session_state.get("session_expired", False)):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Checking customer records and policies…"):
                auth = credentials()
                answer = request("POST", "/chat", credentials=auth,
                    json={"session_id": auth["session_id"], "message": prompt})
            st.session_state.messages.append({"role": "assistant", "content": answer["answer"],
                                              "sources": answer["sources"], "status": answer["status"]})
            st.rerun()
        except APIError as exc:
            show_error(exc)
            st.session_state.messages.append({"role": "assistant", "content": str(exc), "status": "error"})
            st.rerun()
