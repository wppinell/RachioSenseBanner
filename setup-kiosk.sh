#!/bin/bash

###############################################################################
# RachioSense Pi Dashboard - Kiosk Setup Script
# This script configures a Raspberry Pi for headless kiosk mode
# Must be run as root/sudo
###############################################################################

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
  echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
  echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

###############################################################################
# Validation
###############################################################################

if [[ $EUID -ne 0 ]]; then
  log_error "This script must be run as root (use sudo)"
  exit 1
fi

log_info "Starting RachioSense Pi Dashboard setup..."

###############################################################################
# Define variables
###############################################################################

PI_USER="${PI_USER:-pi}"
DASHBOARD_DIR="/home/${PI_USER}/rachiosense-dashboard"
VENV_DIR="${DASHBOARD_DIR}/venv"
SERVICE_NAME="rachiosense-dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Check if pi user exists
if ! id "${PI_USER}" &>/dev/null; then
  log_error "User '${PI_USER}' does not exist"
  exit 1
fi

log_info "Using dashboard directory: ${DASHBOARD_DIR}"
log_info "Using user: ${PI_USER}"

###############################################################################
# System Update and Package Installation
###############################################################################

log_info "Updating system packages..."
apt-get update || {
  log_error "Failed to update package list"
  exit 1
}

log_info "Installing required packages..."
PACKAGES="chromium-browser unclutter xdotool python3-pip python3-venv python3-dev git"

for package in $PACKAGES; do
  if ! dpkg -l | grep -q "^ii  $package"; then
    log_info "Installing $package..."
    apt-get install -y "$package" || {
      log_error "Failed to install $package"
      exit 1
    }
  else
    log_success "$package already installed"
  fi
done

###############################################################################
# Dashboard Directory Setup
###############################################################################

log_info "Setting up dashboard directory..."

if [ ! -d "$DASHBOARD_DIR" ]; then
  mkdir -p "$DASHBOARD_DIR"
  chown "${PI_USER}:${PI_USER}" "$DASHBOARD_DIR"
  log_success "Created $DASHBOARD_DIR"
else
  log_warning "$DASHBOARD_DIR already exists"
fi

# Copy project files if they exist in current directory
if [ -f "requirements.txt" ]; then
  cp requirements.txt "$DASHBOARD_DIR/"
  chown "${PI_USER}:${PI_USER}" "$DASHBOARD_DIR/requirements.txt"
  log_success "Copied requirements.txt"
fi

if [ -f "config.json" ]; then
  cp config.json "$DASHBOARD_DIR/"
  chown "${PI_USER}:${PI_USER}" "$DASHBOARD_DIR/config.json"
  log_success "Copied config.json"
fi

if [ -f ".env.example" ]; then
  cp .env.example "$DASHBOARD_DIR/"
  chown "${PI_USER}:${PI_USER}" "$DASHBOARD_DIR/.env.example"
  log_success "Copied .env.example"
fi

###############################################################################
# Python Virtual Environment Setup
###############################################################################

log_info "Setting up Python virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
  sudo -u "${PI_USER}" python3 -m venv "$VENV_DIR" || {
    log_error "Failed to create virtual environment"
    exit 1
  }
  log_success "Created virtual environment"
else
  log_warning "Virtual environment already exists"
fi

# Install Python dependencies
if [ -f "$DASHBOARD_DIR/requirements.txt" ]; then
  log_info "Installing Python dependencies..."
  sudo -u "${PI_USER}" "$VENV_DIR/bin/pip" install --upgrade pip || {
    log_error "Failed to upgrade pip"
    exit 1
  }
  sudo -u "${PI_USER}" "$VENV_DIR/bin/pip" install -r "$DASHBOARD_DIR/requirements.txt" || {
    log_error "Failed to install Python requirements"
    exit 1
  }
  log_success "Python dependencies installed"
else
  log_warning "requirements.txt not found, skipping pip install"
fi

###############################################################################
# API Credentials Setup
###############################################################################

log_info ""
log_info "========================================"
log_info "API CREDENTIALS CONFIGURATION"
log_info "========================================"

ENV_FILE="$DASHBOARD_DIR/.env"

# Check if .env already exists
if [ -f "$ENV_FILE" ]; then
  log_warning ".env file already exists at $ENV_FILE"
  read -p "Do you want to reconfigure API credentials? (y/n): " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_info "Skipping API credentials setup"
  fi
fi

if [ ! -f "$ENV_FILE" ] || [[ $REPLY =~ ^[Yy]$ ]]; then
  log_info ""
  log_info "Please provide the following information:"
  log_info ""

  # Rachio API Key
  read -p "Rachio API Key: " RACHIO_API_KEY
  while [ -z "$RACHIO_API_KEY" ]; then
    log_warning "Rachio API Key cannot be empty"
    read -p "Rachio API Key: " RACHIO_API_KEY
  done

  # SenseCAP API Key
  read -p "SenseCAP API Key: " SENSECRAFT_API_KEY
  while [ -z "$SENSECRAFT_API_KEY" ]; do
    log_warning "SenseCAP API Key cannot be empty"
    read -p "SenseCAP API Key: " SENSECRAFT_API_KEY
  done

  # SenseCAP API Secret
  read -sp "SenseCAP API Secret: " SENSECRAFT_API_SECRET
  echo
  while [ -z "$SENSECRAFT_API_SECRET" ]; do
    log_warning "SenseCAP API Secret cannot be empty"
    read -sp "SenseCAP API Secret: " SENSECRAFT_API_SECRET
    echo
  done

  # Location (optional, with defaults)
  read -p "Latitude (default: 33.4484): " LATITUDE
  LATITUDE=${LATITUDE:-33.4484}

  read -p "Longitude (default: -112.0740): " LONGITUDE
  LONGITUDE=${LONGITUDE:--112.0740}

  # Port
  read -p "Server port (default: 8090): " PORT
  PORT=${PORT:-8090}

  # Refresh interval
  read -p "Refresh interval in seconds (default: 1800): " REFRESH_INTERVAL
  REFRESH_INTERVAL=${REFRESH_INTERVAL:-1800}

  # Create .env file
  cat > "$ENV_FILE" << EOL
# RachioSense Pi Dashboard Configuration
# Generated by setup-kiosk.sh on $(date)

RACHIO_API_KEY=${RACHIO_API_KEY}
SENSECRAFT_API_KEY=${SENSECRAFT_API_KEY}
SENSECRAFT_API_SECRET=${SENSECRAFT_API_SECRET}

LATITUDE=${LATITUDE}
LONGITUDE=${LONGITUDE}

PORT=${PORT}
REFRESH_INTERVAL=${REFRESH_INTERVAL}
EOL

  chown "${PI_USER}:${PI_USER}" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log_success ".env file created with credentials"
fi

###############################################################################
# Systemd Service Setup
###############################################################################

log_info "Creating systemd service for Flask backend..."

cat > "$SERVICE_FILE" << 'EOF'
[Unit]
Description=RachioSense Pi Dashboard Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/rachiosense-dashboard
ExecStart=/home/pi/rachiosense-dashboard/venv/bin/python3 server.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
log_success "Created systemd service at $SERVICE_FILE"

# Enable the service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" || {
  log_error "Failed to enable $SERVICE_NAME service"
  exit 1
}
log_success "Enabled $SERVICE_NAME service"

###############################################################################
# X11/Display Setup for Pi User
###############################################################################

log_info "Setting up X11 and display configuration..."

AUTOSTART_DIR="/home/${PI_USER}/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
chown "${PI_USER}:${PI_USER}" "$AUTOSTART_DIR"

# Create kiosk startup script
KIOSK_SCRIPT="/home/${PI_USER}/start-kiosk.sh"
cat > "$KIOSK_SCRIPT" << 'EOF'
#!/bin/bash

# Wait for X server to start
for i in {1..30}; do
  if xset q &>/dev/null; then
    break
  fi
  sleep 1
done

# Hide mouse cursor
unclutter -display :0 -idle 1 &

# Start Chromium in kiosk mode
chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-component-extensions-with-background-pages \
  --disable-background-networking \
  --disable-default-apps \
  --disable-preconnect \
  http://localhost:8090
EOF

chmod +x "$KIOSK_SCRIPT"
chown "${PI_USER}:${PI_USER}" "$KIOSK_SCRIPT"
log_success "Created kiosk startup script at $KIOSK_SCRIPT"

# Create desktop entry for autostart
DESKTOP_FILE="${AUTOSTART_DIR}/rachiosense-kiosk.desktop"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=RachioSense Dashboard
Exec=/home/${PI_USER}/start-kiosk.sh
NoDisplay=false
AutostartPhase=PostSession
StartupNotify=false
EOF

chmod 644 "$DESKTOP_FILE"
chown "${PI_USER}:${PI_USER}" "$DESKTOP_FILE"
log_success "Created autostart desktop entry at $DESKTOP_FILE"

###############################################################################
# Display and Screen Blanking Configuration
###############################################################################

log_info "Configuring display settings (disable screen blanking)..."

# Create /etc/lightdm/lightdm.conf.d/ entry to disable screen blanking
mkdir -p /etc/lightdm/lightdm.conf.d/
cat > /etc/lightdm/lightdm.conf.d/90-disable-blanking.conf << 'EOF'
[SeatDefaults]
xserver-command=X -s 0 -dpms
xserver-allow-tcp=false
EOF

log_success "Configured lightdm to disable screen blanking"

# Alternative: disable via xset in the kiosk script (already done above with unclutter timing)

###############################################################################
# Log Rotation Setup
###############################################################################

log_info "Setting up log rotation..."

cat > /etc/logrotate.d/rachiosense-dashboard << 'EOF'
/var/log/rachiosense-dashboard.log {
  daily
  rotate 7
  compress
  delaycompress
  missingok
  notifempty
  create 0640 pi pi
  sharedscripts
}
EOF

chmod 644 /etc/logrotate.d/rachiosense-dashboard
log_success "Configured log rotation"

###############################################################################
# Final Configuration
###############################################################################

log_info "Applying final configurations..."

# Create log directory
mkdir -p /var/log/rachiosense
chown "${PI_USER}:${PI_USER}" /var/log/rachiosense

# Disable automatic screen locking for console
if [ -f "/etc/kbd/config" ]; then
  sed -i 's/^BLANK_TIME=.*/BLANK_TIME=0/' /etc/kbd/config 2>/dev/null || true
  log_success "Updated /etc/kbd/config"
fi

###############################################################################
# Summary and Next Steps
###############################################################################

log_info ""
log_info "========================================"
log_success "Setup Complete!"
log_info "========================================"
log_info ""
log_info "Next steps:"
log_info "1. Copy or create your Flask app (app.py) to: $DASHBOARD_DIR"
log_info "2. Review your configuration:"
log_info "   - .env file: $ENV_FILE"
log_info "   - config.json: $DASHBOARD_DIR/config.json"
log_info ""
log_info "3. Start the service:"
log_info "   sudo systemctl start ${SERVICE_NAME}"
log_info ""
log_info "4. Check service status:"
log_info "   sudo systemctl status ${SERVICE_NAME}"
log_info ""
log_info "5. View service logs:"
log_info "   sudo journalctl -u ${SERVICE_NAME} -f"
log_info ""
log_info "6. For kiosk mode to work, ensure a display manager (lightdm) is installed:"
log_info "   sudo apt-get install lightdm"
log_info ""
log_info "7. Reboot to start the dashboard:"
log_info "   sudo reboot"
log_info ""
log_info "========================================"

exit 0
