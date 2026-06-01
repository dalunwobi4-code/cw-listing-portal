#!/bin/bash
# CW Lagos Listing Portal — One-time installer
# Run this once on each iMac: bash install.sh

set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   CW Lagos Listing Portal — Installer   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 not found. Install it from https://python.org then run this again."
    exit 1
fi
echo "✅ Python $(python3 --version)"

# 2. Install pip packages
echo ""
echo "📦 Installing packages (this takes ~2 minutes)..."
if pip3 install -q -r requirements.txt 2>/dev/null; then
    echo "✅ Packages installed"
elif pip3 install -q -r requirements.txt --break-system-packages 2>/dev/null; then
    echo "✅ Packages installed"
else
    pip3 install -r requirements.txt --user
    echo "✅ Packages installed"
fi

# 3. Create directories
mkdir -p web/uploads watermarked enhanced raw_images logs web/static

# 4. Check credentials file
if [ ! -f "orchestrate.py" ]; then
    echo ""
    echo "⚠️  Missing credentials file (orchestrate.py)"
    echo "   Ask your manager for the orchestrate.py file"
    echo "   and place it in this folder, then run install.sh again."
    echo ""
    exit 1
fi
echo "✅ Credentials found"

# 5. Create double-click launcher
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cat > "$SCRIPT_DIR/CW Listings.command" << LAUNCHER
#!/bin/bash
cd "$SCRIPT_DIR"
echo "╔══════════════════════════════════════════╗"
echo "║     CW Lagos Listing Portal             ║"
echo "║     Starting...                         ║"
echo "╚══════════════════════════════════════════╝"
python3 web/app.py &
sleep 2
open http://localhost:5001
echo ""
echo "Portal running at http://localhost:5001"
echo "Keep this window open. Close it to stop."
wait
LAUNCHER

chmod +x "$SCRIPT_DIR/CW Listings.command"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║           ✅ INSTALL COMPLETE            ║"
echo "╠══════════════════════════════════════════╣"
echo "║  Double-click 'CW Listings.command'     ║"
echo "║  to launch the portal anytime.          ║"
echo "╚══════════════════════════════════════════╝"
echo ""
