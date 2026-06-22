#!/bin/bash
# Claude Code 텔레그램 봇 실행
cd "$(dirname "$0")"
exec ./venv/bin/python bot.py
