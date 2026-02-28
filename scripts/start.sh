#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PORT=8000

# --- Activate conda environment (lerobot) ---
eval "$(conda shell.bash hook)" 2>/dev/null || true
conda activate lerobot 2>/dev/null || echo "Warning: conda env 'lerobot' not found, using system Python"

# --- Generate SSL certs if missing ---
if [ ! -f certs/cert.pem ] || [ ! -f certs/key.pem ]; then
    echo "Generating self-signed SSL certificate..."
    mkdir -p certs

    LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")

    openssl req -x509 -newkey rsa:2048 \
        -keyout certs/key.pem \
        -out certs/cert.pem \
        -days 365 -nodes \
        -subj "/CN=Remote CLI Bridge" \
        -addext "subjectAltName=IP:${LOCAL_IP},DNS:localhost,IP:127.0.0.1" \
        2>/dev/null

    echo "SSL certificate generated for IP: ${LOCAL_IP}"
    echo ""
fi

# --- Get network info ---
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")
URL="https://${LOCAL_IP}:${PORT}"

echo "============================================"
echo "  Remote CLI Bridge"
echo "============================================"
echo ""
echo "  Local:   https://localhost:${PORT}"
echo "  Network: ${URL}"
echo ""

# --- QR Code ---
python3 -c "
import qrcode
qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
qr.add_data('${URL}')
qr.print_ascii(invert=True)
" 2>/dev/null && echo "" || echo "  (Install qrcode for QR: pip install qrcode)"

echo "  Scan the QR code with your phone camera."
echo "  Accept the self-signed certificate when prompted."
echo ""
echo "  Press Ctrl+C to stop."
echo "============================================"
echo ""

# --- Start server ---
python3 -m server.main
