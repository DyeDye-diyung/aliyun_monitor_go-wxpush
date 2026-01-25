#!/bin/bash
# Alpine to Debian 13 (Static IP Fix Version)
# Auto extract network config -> Static IP install -> Prevent disconnect after reboot
# -------------------------------------------------------------

# --- 1. Install dependencies ---
if [ ! -f /bin/bash ]; then
    echo "Installing bash..."
    apk update >/dev/null 2>&1
    apk add bash iproute2 grep gawk ipcalc >/dev/null 2>&1
fi

set -e

# --- 2. Root check ---
if [ "$(id -u)" != "0" ]; then
    echo "Error: Must run as root."
    exit 1
fi

# --- 3. Interactive config ---
clear
echo "=== Alpine to Debian 13 Auto Install Script ==="
echo "Script will extract current IP config for static IP install."
echo ""

# Use compatible input method
if [ -z "$PORT" ]; then
    read -p "SSH Port [default 22]: " PORT
    PORT=${PORT:-22}
fi
if [ -z "$PASSWORD" ]; then
    read -p "Root Password [default yiwan123]: " PASSWORD
    PASSWORD=${PASSWORD:-yiwan123}
fi

echo ""
echo "Config confirmed: Port $PORT / Password $PASSWORD"
echo "Starting auto install in 5 seconds..."
sleep 5

# --- 4. Main logic ---

echo "[1/5] Extracting network config..."
# Get main interface (usually eth0)
MAIN_IFace=$(ip route show default | awk '{print $5}' | head -n1)

# Get IP address (e.g. 192.168.1.100)
MAIN_IP=$(ip -4 addr show $MAIN_IFace | awk '/inet / {print $2}' | cut -d/ -f1 | head -n 1)

# Get gateway (e.g. 192.168.1.1)
MAIN_GATE=$(ip route show default | awk '/default/ {print $3}')

# Get CIDR number (e.g. 24)
CIDR_NUM=$(ip -4 addr show $MAIN_IFace | awk '/inet / {print $2}' | cut -d/ -f2 | head -n 1)

echo "Detected network config:"
echo "IP: $MAIN_IP"
echo "Gateway: $MAIN_GATE"
echo "CIDR: /$CIDR_NUM"

# Validate network config
if [ -z "$MAIN_IP" ] || [ -z "$MAIN_GATE" ] || [ -z "$CIDR_NUM" ]; then
    echo "Error: Failed to extract network config!"
    echo "Please check your network connection."
    exit 1
fi

echo "[2/5] Cleaning disk signatures..."
sed -i 's/^#\(.*community\)$/\1/' /etc/apk/repositories
apk update >/dev/null 2>&1
apk add curl util-linux parted e2fsprogs grub grub-bios wget >/dev/null 2>&1
umount /boot 2>/dev/null || true
swapoff -a 2>/dev/null || true

DISK="/dev/vda"
B=$(basename $DISK)

dd if=/dev/zero of=$DISK bs=1K seek=32 count=992 conv=notrunc status=none
dd if=/dev/zero of=$DISK bs=512 seek=1 count=33 conv=notrunc status=none
SECTORS=$(cat /sys/block/$B/size)
dd if=/dev/zero of=$DISK bs=512 seek=$((SECTORS-33)) count=33 conv=notrunc status=none
sync

echo "[3/5] Fixing MBR bootloader..."
# Try mount, failure won't affect subsequent DD
mount ${DISK}1 /boot 2>/dev/null || true
grub-install --recheck $DISK >/dev/null 2>&1 || true

echo "[4/5] Downloading install script..."
wget --no-check-certificate -qO InstallNET.sh 'https://raw.githubusercontent.com/leitbogioro/Tools/master/Linux_reinstall/InstallNET.sh' && chmod a+x InstallNET.sh

echo "[5/5] Starting Debian installer (Static IP mode)..."
echo "System will reboot. Please wait 10-15 minutes then login with new password."

# Run InstallNET with static IP parameters
bash InstallNET.sh \
    -debian 13 \
    -port "${PORT}" \
    -pwd "${PASSWORD}" \
    -mirror "http://deb.debian.org/debian/" \
    --ip-addr "${MAIN_IP}" \
    --ip-gate "${MAIN_GATE}" \
    --ip-mask "${CIDR_NUM}" \
    -swap "512" \
    --cloudkernel "0" \
    --bbr \
    --motd \
    --setdns "1.1.1.1,8.8.8.8"

# Force reboot
reboot
