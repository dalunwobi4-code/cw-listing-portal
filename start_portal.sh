#!/bin/bash
# CW Lagos Listing Portal — run this to start the web interface
cd "$(dirname "$0")"
echo ""
echo "  CW Lagos Listing Portal"
echo "  Open in your browser: http://localhost:5001"
echo "  Press Ctrl+C to stop"
echo ""
python3 web/app.py
