# Cisco IOS-XE Zero Touch Provisioning (ZTP)

A production-ready ZTP script for **Day 0** provisioning of Cisco Catalyst switches running IOS-XE.
Automatically downloads and applies device-specific configurations when a switch boots without a
startup-config, enabling **hands-free** deployment at scale.

> **Version:** V2.1 | **Author:** Chris Davies

---

## Highlights

- **Serial-based configuration lookup** — Each device fetches its own config (`<SERIAL>.txt`) from a TFTP server.
- **Automatic fallback** — Falls back to a site-specific default config if no per-serial file is found.
- **Multi-site support** — A single TFTP server serves all sites; each site has its own folder containing its script, device configs, and reports. Set `SITE_CODE` and point DHCP Option 67 to `<SITE_CODE>/day_0_provisioning.py`.
- **Retry logic with exponential backoff** — Handles transient network issues gracefully (4 attempts, capped at 30 s).
- **Automatic SSH key generation** — Generates 2048-bit RSA keys if none are present, enabling SSH access immediately post-provisioning.
- **Rotating file logs on flash** — Persistent local log (5 MB max, 10 backups) written to `/flash/guest-share/ztp.log`.
- **Automatic log upload** — Logfile is always uploaded to the TFTP server on exit, regardless of success or failure.
- **Secure file cleanup** — Downloaded config file is deleted from flash after merging.
- **stdlib only** — No third-party packages; runs cleanly inside Guestshell.

---

## Repository layout

```
.
└── day_0_provisioning.py    # Master ZTP script — copy into each site folder with SITE_CODE set
```

### TFTP server structure

#### Single-site (`SITE_CODE = ""`)

```
TFTP Root/
├── day_0_provisioning.py        # Script (SITE_CODE = "")
├── FCW2144L045.txt              # Per-serial config files
├── config.txt                   # Default/fallback config
└── ZTP_Reports/
    └── ztp-<SERIAL>-<STATUS>-<EPOCH>.log
```

#### Multi-site (`SITE_CODE = "SITE_A"`, etc.)

```
TFTP Root/
├── SITE_A/
│   ├── day_0_provisioning.py    # Script with SITE_CODE = "SITE_A"
│   ├── FCW2144L045.txt          # Site A device configs
│   ├── config.txt               # Site A fallback (Site A addressing)
│   └── ZTP_Reports/
│       └── ztp-<SERIAL>-<STATUS>-<EPOCH>.log
├── SITE_B/
│   ├── day_0_provisioning.py    # Script with SITE_CODE = "SITE_B"
│   ├── FDO2129Y06B.txt          # Site B device configs
│   ├── config.txt               # Site B fallback (Site B addressing)
│   └── ZTP_Reports/
│       └── ztp-<SERIAL>-<STATUS>-<EPOCH>.log
└── SITE_C/
    └── ...
```

> Configuration files **must** be named `<SERIAL_NUMBER>.txt`, exactly matching the device's system
> serial number. The script queries `show version` to extract the serial and builds the filename
> automatically. If no per-serial file is found, the script falls back to the site's `config.txt`.

---

## Requirements

- **Python:** 3.7+ (included in IOS-XE Guestshell — no installation needed)
- **Python packages:** stdlib only — `re`, `time`, `sys`, `logging` plus the IOS-XE `cli` module
- **Network infrastructure:**
  - DHCP server with **Option 66** (TFTP server IP) and **Option 67** (ZTP script filename)
  - TFTP server hosting the script and device-specific configuration files

### Supported devices

- Cisco Catalyst 9000 Series (9200, 9300, 9400, 9500, 9600)
- IOS-XE 16.x and 17.x
- Any Cisco device with Guestshell and native ZTP support

---

## Configuration

All tunable settings are constants at the top of `day_0_provisioning.py`:

| Constant | Purpose | Default |
|---|---|---|
| `TFTP_SERVER` | Shared TFTP server IP (used by all sites) | `192.0.2.1` |
| `SITE_CODE` | Site folder name on the TFTP server. Drives all remote paths. Leave `""` for single-site. | `""` |
| `DEFAULT_CONFIG_FILENAME` | Fallback config if no per-serial file exists | `config.txt` |
| `CONFIG_REMOTE_DIR` | Derived from `SITE_CODE` — do not edit directly | *(auto)* |
| `ENABLE_LOG_UPLOAD` | Upload the logfile to TFTP on exit | `True` |
| `LOG_UPLOAD_SERVER` | TFTP server to receive the logfile | *(same as `TFTP_SERVER`)* |
| `LOG_UPLOAD_REMOTE_DIR` | Derived from `SITE_CODE` — do not edit directly | *(auto)* |
| `LOG_UPLOAD_NAME_TEMPLATE` | Logfile naming template | `ztp-{serial}-{status}-{epoch}.log` |
| `LOG_TO_FILE` | Enable persistent logging to flash | `True` |
| `LOG_PATH_PRIMARY` | Primary log path (Guestshell guest-share) | `/flash/guest-share/ztp.log` |
| `LOG_PATH_FALLBACK` | Fallback log path if primary is unavailable | `/flash/ztp.log` |
| `MAX_COPY_ATTEMPTS` | TFTP transfer retry attempts | `4` |
| `BASE_BACKOFF_SECONDS` | Base delay for exponential backoff | `2` |
| `WRITE_MEMORY` | Save to startup-config after merge | `True` |

### Minimum configuration — single site

```python
TFTP_SERVER = "192.168.1.10"  # Your TFTP server IP
SITE_CODE   = ""              # Leave empty; files sit at the TFTP root
```

### Minimum configuration — multi-site

```python
TFTP_SERVER = "192.168.1.10"  # Shared TFTP server (same for all sites)
SITE_CODE   = "SITE_A"        # Must match the site folder name on the TFTP server
```

With `SITE_CODE = "SITE_A"` the script will automatically:
- Download configs from `SITE_A/<SERIAL>.txt`
- Fall back to `SITE_A/config.txt`
- Upload logs to `SITE_A/ZTP_Reports/`

---

## How to deploy

### Step 1: Find device serial numbers

```
Switch# show version | include Serial
System Serial Number : FCW2144L045
```

### Step 2: Create per-device configuration files

Name each file exactly as `<SERIAL_NUMBER>.txt`:

```
FCW2144L045.txt
FDO2129Y06B.txt
FOC2201X0QY.txt
```

Example content:

```
hostname SW-ACCESS-01
!
enable secret YourEnablePassword
username admin privilege 15 secret YourAdminPassword
!
ip domain-name company.local
!
vlan 10
 name MGMT
!
interface Vlan10
 description Management VLAN
 ip address 10.1.10.50 255.255.255.0
!
ip default-gateway 10.1.10.1
!
line vty 0 15
 transport input ssh
 login local
!
end
```

### Step 3: Set up the TFTP server

Create a folder per site at the TFTP root and place all site files inside it:

```
/tftpboot/SITE_A/day_0_provisioning.py   ← SITE_CODE = "SITE_A"
/tftpboot/SITE_A/FCW2144L045.txt
/tftpboot/SITE_A/config.txt              ← fallback for Site A
/tftpboot/SITE_B/day_0_provisioning.py   ← SITE_CODE = "SITE_B"
/tftpboot/SITE_B/FDO2129Y06B.txt
/tftpboot/SITE_B/config.txt              ← fallback for Site B
```

Ensure the TFTP daemon has read access to all files and write access to each
`<SITE_CODE>/ZTP_Reports/` directory (for log uploads).

### Step 4: Configure DHCP Options 66 and 67

The switch requires two DHCP options: **Option 66** carries the TFTP server IP address and
**Option 67** carries the path to the ZTP script. The switch combines these at boot time to
fetch the script via TFTP.

For multi-site deployments, **Option 66 is the same for all sites** (shared TFTP server).
**Option 67 differs per site** — it includes the site folder path.

#### ISC DHCP (Linux) — multi-site example

```
# Site A scope
subnet 10.1.1.0 netmask 255.255.255.0 {
    range 10.1.1.100 10.1.1.200;
    option routers 10.1.1.1;
    option tftp-server-name "192.168.1.10";              # Option 66 — shared TFTP server
    option bootfile-name "SITE_A/day_0_provisioning.py"; # Option 67 — path to Site A script
}

# Site B scope
subnet 10.2.1.0 netmask 255.255.255.0 {
    range 10.2.1.100 10.2.1.200;
    option routers 10.2.1.1;
    option tftp-server-name "192.168.1.10";              # Option 66 — shared TFTP server
    option bootfile-name "SITE_B/day_0_provisioning.py"; # Option 67 — path to Site B script
}
```

#### Windows DHCP — multi-site example

```powershell
# Site A scope
Set-DhcpServerv4OptionValue -OptionId 66 -Value "192.168.1.10" -ScopeId 10.1.1.0
Set-DhcpServerv4OptionValue -OptionId 67 -Value "SITE_A/day_0_provisioning.py" -ScopeId 10.1.1.0

# Site B scope
Set-DhcpServerv4OptionValue -OptionId 66 -Value "192.168.1.10" -ScopeId 10.2.1.0
Set-DhcpServerv4OptionValue -OptionId 67 -Value "SITE_B/day_0_provisioning.py" -ScopeId 10.2.1.0
```

### Step 5: Deploy

1. For each site, copy `day_0_provisioning.py` into the site folder on the TFTP server and set `SITE_CODE` to match the folder name
2. Place device config files (`<SERIAL>.txt`) and a `config.txt` fallback in the same site folder
3. Configure the site's DHCP scope: Option 66 = TFTP server IP, Option 67 = `<SITE_CODE>/day_0_provisioning.py`
4. Connect the switch to the network
5. Power on — ZTP runs automatically on first boot (no startup-config)

---

## ZTP workflow

```
Switch boots (no startup-config)
        │
        ▼
DHCP: receives IP + Option 66 (TFTP server IP) + Option 67 (site path to script)
        │
        ▼
IOS-XE downloads & executes day_0_provisioning.py in Guestshell
        │
        ├─ 1. Initialise logging (console + rotating file on flash)
        ├─ 2. Enable 'file prompt quiet'
        ├─ 3. Collect device facts (serial, hostname, model, version)
        ├─ 4. TFTP download: <SERIAL>.txt  ──(not found?)──▶  config.txt
        ├─ 5. Merge into running-config
        ├─ 6. Save to startup-config  (if WRITE_MEMORY = True)
        ├─ 7. Delete downloaded file from flash
        ├─ 8. Generate RSA 2048-bit SSH keys  (if not already present)
        ├─ 9. Re-detect management IP
        ├─ 10. Append summary report to logfile
        └─ 11. Upload logfile to TFTP  (always — success or failure)
```

**Retry schedule (TFTP transfers):**
Attempt 1 → wait 2 s → Attempt 2 → wait 4 s → Attempt 3 → wait 8 s → Attempt 4

---

## Output & logs

### Console / log output (typical successful run)

```
2026-05-13 10:01:00 :: INFO :: ###### STARTING ZTP SCRIPT ######
2026-05-13 10:01:01 :: INFO :: ### Attempting to retrieve device serial number
2026-05-13 10:01:03 :: INFO :: ### Attempting download of config file: FCW2144L045.txt
2026-05-13 10:01:04 :: INFO :: **** TFTP copy attempt 1/4: tftp://192.168.1.10/SITE_A/FCW2144L045.txt -> flash:FCW2144L045.txt ****
2026-05-13 10:01:06 :: INFO :: **** Finished transferring device configuration file ****
2026-05-13 10:01:06 :: INFO :: ********** Merging running-config with downloaded file **********
2026-05-13 10:01:09 :: INFO :: ### Writing configuration to startup-config
2026-05-13 10:01:11 :: INFO :: ### Deleting downloaded config file from flash
2026-05-13 10:01:11 :: INFO :: ###### Checking for existing RSA SSH keys ######
2026-05-13 10:01:11 :: INFO :: ###### Generating SSH Keys... This may take a few minutes ######
2026-05-13 10:01:38 :: INFO :: ###### SSH Key Generation Complete ######
2026-05-13 10:01:38 :: INFO :: ========== ZTP SUMMARY REPORT ==========
2026-05-13 10:01:38 :: INFO :: ZTP Status       : SUCCESS
2026-05-13 10:01:38 :: INFO :: Hostname         : SW-ACCESS-01
2026-05-13 10:01:38 :: INFO :: Serial           : FCW2144L045
2026-05-13 10:01:38 :: INFO :: Model            : C9300-48U
2026-05-13 10:01:38 :: INFO :: Software         : 17.9.4
2026-05-13 10:01:38 :: INFO :: Mgmt IP (VRF)    : 10.1.10.50 (Mgmt-vrf)
2026-05-13 10:01:38 :: INFO :: ========== END ZTP SUMMARY REPORT ======
2026-05-13 10:01:38 :: INFO :: ###### END OF ZTP SCRIPT ######
```

### Log file location on device

| Path | Notes |
|---|---|
| `/flash/guest-share/ztp.log` | Primary — used when Guestshell guest-share is available |
| `/flash/ztp.log` | Fallback |

Rotation: 5 MB per file, 10 backups retained.

### Uploaded logfile naming

```
ztp-FCW2144L045-SUCCESS-1747131698.log
ztp-FCW2144L045-FAILED-1747131698.log
```

Uploaded to: `tftp://<LOG_UPLOAD_SERVER>/<SITE_CODE>/ZTP_Reports/`

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| ZTP does not start | DHCP Options 66/67 not set | Verify with `show dhcp lease` on the switch |
| `%Error` during config download | File missing or wrong name | Confirm filename matches serial exactly (`<SERIAL>.txt`) |
| TFTP timeout on all 4 attempts | Network/firewall blocking UDP 69 | Verify TFTP reachability from the switch management network |
| Merge fails with error | Syntax error in config file | Test the config file on a lab device first |
| SSH not working after ZTP | Keys not generated | Check `show crypto key mypubkey rsa` |
| Logfile not uploaded | TFTP server has no write permission | Ensure the TFTP daemon allows uploads to `<SITE_CODE>/ZTP_Reports/` |

### Useful verification commands

```
! Check DHCP lease and Options 66/67
Switch# show dhcp lease

! Test TFTP reachability
Switch# copy tftp://192.168.1.10/SITE_A/config.txt flash:test.cfg

! Check serial number
Switch# show version | include Serial

! Review the local ZTP log
Switch# more flash:guest-share/ztp.log

! Check RSA keys
Switch# show crypto key mypubkey rsa
```

---

## Security considerations

- **Config files contain credentials** — The downloaded `.txt` file is deleted from flash immediately
  after merging. Store files securely on the TFTP server with restricted read permissions.
- **TFTP is unauthenticated and unencrypted** — Deploy on an isolated management or staging VLAN;
  do not expose the TFTP server to untrusted networks.
- **DHCP Option 66/67 spoofing** — Enable DHCP snooping on access switches; use dedicated, trusted
  DHCP servers for ZTP VLANs.
- **Disable ZTP after provisioning** — The presence of a startup-config prevents re-ZTP on reboot,
  but you can also explicitly disable it:
  ```
  no service dhcp
  ```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | ZTP completed successfully |
| `1` | ZTP failed — check the logfile for details |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Free to use, modify and redistribute. If you'd like it tailored, deployed or extended for your estate, see [Work With Me](https://www.nautomationprime.io/work-with-me/).

