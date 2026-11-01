"""Realtime service (limo-rt): WebSocket fan-out, ride status writes, push and chat.

Runs as its own process (``uvicorn --factory app.realtime.main:create_app``), separate
from the AI chatbot, with its own database user. The chatbot never imports this package.
"""
