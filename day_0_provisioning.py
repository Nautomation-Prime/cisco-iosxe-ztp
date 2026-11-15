#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ZTP Script for Cisco IOS-XE Devices (Guestshell-safe, stdlib only)

Summary
-------
- Fetch per-serial config via TFTP (DHCP Opt 66 = TFTP server IP, Opt 67 = site path to script).
- Site-aware folder layout — set SITE_CODE to isolate each site's configs and logs.
- Merge into running-config; optionally save to startup-config.
- Generate RSA SSH keys if missing (idempotent).
- Robust logging to /flash with rotation.
- Build a small ZTP report and append it into the logfile.

Notes
-----
- Uses only Python stdlib + IOS 'cli' integration, so it runs cleanly
  inside Guestshell without extra packages.

Author: Chris Davies
Version: V2.1
"""
import re
import time
import sys
import logging
from logging.handlers import RotatingFileHandler

# Cisco IOS-XE Python hooks (available in Guestshell environment)
from cli import configure, cli, executep

# =============================================================================
# Core settings
# =============================================================================

# TFTP server IP (shared across all sites)
TFTP_SERVER = "192.0.2.1"  # <-- UPDATE THIS TO YOUR TFTP SERVER IP

# Site code — must match the site folder name on the TFTP server.
# DHCP Option 67 for this site should be: <SITE_CODE>/day_0_provisioning.py
# Leave empty ("") only for a flat single-site deployment.
# Example: SITE_CODE = "SITE_A"  ->  all files live under TFTP_ROOT/SITE_A/
SITE_CODE = ""  # <-- SET THIS TO YOUR SITE FOLDER NAME

# All remote paths derive from SITE_CODE automatically — do not edit these directly.
_site_dir = SITE_CODE.strip("/") if SITE_CODE else ""

# Config fallback (used if no per-serial file is found)
DEFAULT_CONFIG_FILENAME = "config.txt"

# Remote directory on the TFTP server where config files are stored
CONFIG_REMOTE_DIR = f"ZTP_Files/{_site_dir}"  # e.g., "SITE_A" or "" for root

# Upload the ZTP logfile to TFTP when the script finishes
ENABLE_LOG_UPLOAD = True
LOG_UPLOAD_SERVER = TFTP_SERVER  # defaults to same server used for config pulls
LOG_UPLOAD_REMOTE_DIR = f"ZTP_Files/{_site_dir}/ZTP_Reports" if _site_dir else "ZTP_Reports"
LOG_UPLOAD_NAME_TEMPLATE = "ztp-{serial}-{status}-{epoch}.log"

# Logging
LOG_TO_FILE = True
LOG_PATH_PRIMARY = "/flash/guest-share/ztp.log"
LOG_PATH_FALLBACK = "/flash/ztp.log"

# Copy retry policy
MAX_COPY_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 2

# Save running-config after merge
WRITE_MEMORY = True


# =============================================================================
# Main entry point
# =============================================================================
def main():
    """
    Entry point for ZTP workflow.

    Steps
    -----
    1. Initialize logging (console + rotating file in /flash).
    2. Enable 'file prompt quiet' to avoid interactive copy prompts.
    3. Collect initial device facts (serial, hostname, model, version, IP best-effort).
    4. Fetch config by serial via TFTP; if missing, fall back to DEFAULT_CONFIG_FILENAME.
    5. Merge into running-config; optionally save to startup-config.
    6. Clean up the downloaded file from flash:.
    7. Ensure RSA SSH keys exist (generate if missing).
    8. Re-detect management IP (it may become available after merge).
    9. Append a summary report into the logfile.
    10. Always upload the local logfile to the TFTP server on exit (success or failure).
    """
    # Initialize logging early
    log_os_path = create_logfile() if LOG_TO_FILE else None
    configure_logger(log_os_path)

    rc = 1
    facts = {}

    try:
        log_info("###### STARTING ZTP SCRIPT ######")

        _enable_file_prompt_quiet()

        # Collect initial facts (serial/hostname/version/model/IP best-effort)
        facts = collect_device_facts(safe_mode=True)

        # ------------------------------------------------------------------
        # Fetch device-specific config (serial-based) with fallback
        # ------------------------------------------------------------------
        primary_cfg = f"{facts['serial']}.txt"
        chosen_cfg = primary_cfg

        log_info(f"### Attempting download of config file: {primary_cfg}")
        copied = file_transfer(TFTP_SERVER, primary_cfg)

        if not copied:
            log_info(
                f"### Primary config '{primary_cfg}' not available; "
                f"falling back to '{DEFAULT_CONFIG_FILENAME}'"
            )
            chosen_cfg = DEFAULT_CONFIG_FILENAME
            copied = file_transfer(TFTP_SERVER, chosen_cfg)

        if not copied:
            raise RuntimeError(
                "Neither the per-serial config nor the default config could be downloaded."
            )

        facts["config_file"] = chosen_cfg

        # Merge into running-config
        configure_merge(chosen_cfg)

        # Optionally save to startup-config
        if WRITE_MEMORY:
            _save_running_to_startup()

        # Cleanup downloaded file
        _cleanup_downloaded_file(chosen_cfg)

        # Generate SSH RSA keys if missing (idempotent)
        ensure_rsa_keys()

        # Update values after merge.
        new_facts = collect_device_facts(safe_mode=False)
        for k, v in new_facts.items():
            if v:  # only overwrite with non-empty values
                facts[k] = v


        # Append summary report into the logfile (single artifact)
        facts["ztp_status"] = "SUCCESS"
        append_report_to_log(facts)
        log_info("###### END OF ZTP SCRIPT ######")
        rc = 0

    except Exception as exc:  # noqa: BLE001
        facts["ztp_status"] = "FAILED"
        facts["error"] = str(exc)

        log_critical("*** Failure during day-0 provisioning. Aborting ZTP. ***")
        log_critical(str(exc))

        # Always try to push failure report for triage
        try:
            if not facts.get("serial"):
                facts.update(collect_device_facts(safe_mode=True))
            append_report_to_log(facts)
        except Exception:
            pass

        log_info("###### END OF ZTP SCRIPT (FAILED) ######")
        rc = 1

    finally:
        # Close file handlers so the logfile is complete before uploading
        try:
            _close_file_log_handlers()
        except Exception:
            pass

        # Always attempt to upload the logfile (success or failure)
        try:
            upload_logfile_to_tftp(log_os_path, facts)
        except Exception as exc:  # noqa: BLE001
            print("Log upload failed: " + str(exc))

    sys.exit(rc)


# =============================================================================
# Helpers - CLI convenience
# =============================================================================

def _enable_file_prompt_quiet():
    """
    Enable IOS-XE 'file prompt quiet' to suppress interactive copy prompts.

    Notes
    -----
    - This is a best-effort setting; failure is non-fatal.
    """
    try:
        configure("file prompt quiet")
    except Exception as exc:  # noqa: BLE001
        log_info(f"Non-fatal: could not set file prompt quiet: {exc}")


def _save_running_to_startup():
    """
    Save running-config to startup-config using IOS 'copy' via executep().

    Notes
    -----
    - Failure to save is logged but not considered fatal.
    """
    try:
        log_info("### Writing configuration to startup-config")
        executep("copy running-config startup-config")
    except Exception as exc:  # noqa: BLE001
        log_info(f"Non-fatal: write memory failed: {exc}")


def _cleanup_downloaded_file(filename):
    """
    Delete the previously downloaded config file from flash:.

    Parameters
    ----------
    filename : str
        The name of the file present in flash:.

    Notes
    -----
    - Failure to delete is logged but non-fatal.
    """
    try:
        log_info("### Deleting downloaded config file from flash")
        cli(f"delete /force flash:{filename}")
    except Exception as exc:  # noqa: BLE001
        log_info(f"Non-fatal: could not delete flash:{filename}: {exc}")


# =============================================================================
# Device facts / parsing
# =============================================================================

def collect_device_facts(safe_mode):
    """
    Collect basic device facts: serial, hostname, version, model, mgmt IP/VRF.

    Parameters
    ----------
    safe_mode : bool
        If True, minimize CLI interactions to avoid cascading failures in
        exception paths.

    Returns
    -------
    dict
        Dictionary with keys:
        - serial, hostname, version, model
        - ip, vrf
        - ztp_status (default 'SUCCESS')
    """
    facts = {
        "serial": "",
        "hostname": "",
        "version": "",
        "model": "",
        "ip": "",
        "vrf": "",
        "ztp_status": "SUCCESS",
    }

    # Serial
    try:
        facts["serial"] = get_serial()
    except Exception as exc:  # noqa: BLE001
        facts["serial"] = f"UNKNOWN({exc})"

    # Hostname
    try:
        out = cli("show running-config | include ^hostname ")
        match = re.search(r"^hostname\s+(\S+)", out, re.MULTILINE)
        if match:
            facts["hostname"] = match.group(1)
        else:
            out2 = cli("show version | include uptime is")
            match2 = re.search(r"^(\S+)\s+uptime is", out2, re.MULTILINE)
            if match2:
                facts["hostname"] = match2.group(1)
    except Exception:
        pass

    # Version & Model
    try:
        ver = cli("show version")
        vm = re.search(r"Cisco IOS XE Software, Version\s+([^\s,]+)", ver)
        if vm:
            facts["version"] = vm.group(1)
        mm = re.search(r"cisco\s+(\S+)\s+\(", ver, re.IGNORECASE)
        if mm:
            facts["model"] = mm.group(1)
    except Exception:
        pass

    # IP (and VRF)
    if not safe_mode:
        try:
            facts.update(detect_mgmt_ip())
        except Exception:
            pass

    return facts


def detect_mgmt_ip():
    """
    Attempt to detect a usable management IP address and VRF.

    Strategy
    --------
    1. Check 'Mgmt-vrf' via: show ip interface brief vrf Mgmt-vrf
    2. Fallback to:          show ip interface brief vrf all
    3. Fallback to:          show ip interface brief (default VRF)

    Returns
    -------
    dict
        Dictionary with keys 'ip' and 'vrf'. Empty strings if not found.
    """
    # Try Mgmt-vrf first
    try:
        out = cli("show ip interface brief vrf Mgmt-vrf")
        ip = parse_first_ip_from_brief(out)
        if ip:
            return {"ip": ip, "vrf": "Mgmt-vrf"}
    except Exception:
        pass

    # Fallback: all VRFs, then default
    try:
        out = cli("show ip interface brief vrf all")
        ip = parse_first_ip_from_brief(out)
        if ip:
            return {"ip": ip, "vrf": "unknown"}
    except Exception:
        pass

    try:
        out = cli("show ip interface brief")
        ip = parse_first_ip_from_brief(out)
        if ip:
            return {"ip": ip, "vrf": "default"}
    except Exception:
        pass

    return {"ip": "", "vrf": ""}


def parse_first_ip_from_brief(text):
    """
    Parse 'show ip interface brief*' output and select the first 'up/up' IPv4.

    Parameters
    ----------
    text : str
        CLI output to parse.

    Returns
    -------
    Optional[str]
        First detected IPv4 address on an interface that appears up/up,
        or None if not found.
    """
    lines = text.splitlines()
    for ln in lines:
        if "unassigned" in ln.lower():
            continue
        match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", ln)
        if not match:
            continue
        tokens = ln.lower().split()
        # Heuristic for up/up across spacing differences
        if "up" in tokens[-1:] and ("up" in tokens[-2:-1] or "up" in tokens[-3:-2]):
            return match.group(1)
        if ln.count(" up ") >= 2:
            return match.group(1)
    return None


def get_serial():
    """
    Retrieve the device serial number using robust regex patterns.

    Returns
    -------
    str
        Serial number string.

    Raises
    ------
    RuntimeError
        If the serial cannot be determined from 'show version'.
    """
    log_info("### Attempting to retrieve device serial number")
    show_version = None
    try:
        show_version = cli("show version")
    except Exception as exc:  # noqa: BLE001
        log_info(f"show version failed ({exc}); retrying after 90s...")
        time.sleep(90)
        show_version = cli("show version")

    patterns = [
        r"System Serial Number\s*:\s*(\S+)",
        r"Processor board ID\s+(\S+)",
        r"Chassis Serial Number\s*:\s*(\S+)",
    ]
    for pat in patterns:
        match = re.search(pat, show_version or "", re.IGNORECASE)
        if match:
            return match.group(1)
    raise RuntimeError("Unable to determine serial number from 'show version' output.")


# =============================================================================
# Copy/merge & keys
# =============================================================================

def configure_merge(filename, file_system="flash:/"):
    """
    Merge a file from the specified filesystem into running-config.

    Parameters
    ----------
    filename : str
        Name of the file to merge (e.g., '<serial>.txt').
    file_system : str, optional
        IOS filesystem prefix (default 'flash:/').

    Raises
    ------
    RuntimeError
        If the merge operation reports an IOS error.
    """
    log_info("********** Merging running-config with downloaded file **********")
    cmd = f"copy {file_system}{filename} running-config"
    try:
        out = cli(cmd)
        out_s = str(out)
        if any(token in out_s for token in ("%Error", "Invalid", "No such file")):
            raise RuntimeError(f"Merge reported error: {out_s}")
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to merge configuration: {exc}") from exc


def file_transfer(server, filename):
    """
    Download a file from a TFTP server to flash:, with retries and backoff.

    Parameters
    ----------
    server : str
        TFTP server IP or hostname.
    filename : str
        File name to fetch (e.g., 'ABC123DEF.txt').

    Returns
    -------
    bool
        True if IOS 'copy' indicates a successful transfer; False otherwise.
    """
    remote_dir = (CONFIG_REMOTE_DIR or "").strip("/")
    url = f"tftp://{server}/{filename}" if not remote_dir else f"tftp://{server}/{remote_dir}/{filename}"
    dest = f"flash:{filename}"
    attempt = 0
    while attempt < MAX_COPY_ATTEMPTS:
        attempt += 1
        try:
            log_info(f"**** TFTP copy attempt {attempt}/{MAX_COPY_ATTEMPTS}: {url} -> {dest} ****")
            out = cli(f"copy {url} {dest}")
            out_str = str(out)
            if is_copy_success(out_str):
                log_info("**** Finished transferring device configuration file ****")
                return True
            log_info("Copy output did not indicate success; will retry.")
            log_info(f"Copy output:\n{out_str}")
        except Exception as exc:  # noqa: BLE001
            log_info(f"Copy attempt {attempt} raised exception: {exc}")

        # Exponential backoff, capped
        sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        time.sleep(min(sleep_s, 30))
    return False


def is_copy_success(output):
    """
    Heuristic check to determine if an IOS 'copy' succeeded.

    Parameters
    ----------
    output : str
        Raw output text from IOS copy command.

    Returns
    -------
    bool
        True if 'copy' appears successful; False otherwise.
    """
    output_lower = output.lower()
    success_tokens = [
        "copied",
        "bytes copied",
        "[ok]",
        "success",
        ]
    error_tokens = [
        "%error",
        "invalid",
        "no such file",
        "not found",
        "connection timed out",
        "connection refused",
        "failed",
        ]
    if any(t in output_lower for t in error_tokens):
        return False
    return any(t in output_lower for t in success_tokens)


def ensure_rsa_keys():
    """
    Generate RSA SSH host keys if none are present.

    Notes
    -----
    - Uses 'show crypto key mypubkey rsa' to check.
    - If missing, runs 'crypto key generate rsa modulus 2048'.
    - Errors are logged but non-fatal to continue ZTP.
    """
    try:
        log_info("###### Checking for existing RSA SSH keys ######")
        out = cli("show crypto key mypubkey rsa")
        if "No keys found" in out or "not found" in out.lower() or not out.strip():
            log_info("###### Generating SSH Keys... This may take a few minutes ######")
            configure("crypto key generate rsa modulus 2048")
            log_info("###### SSH Key Generation Complete ######")
        else:
            log_info("###### RSA keys already present. Skipping generation. ######")
    except Exception as exc:  # noqa: BLE001
        log_info(f"Failed to verify/generate crypto keys. SSH access may be impacted. Error: {exc}")


# =============================================================================
# Logging
# =============================================================================

def create_logfile():
    """
    Create a persistent logfile, preferring guest-share and falling back to flash.

    Returns
    -------
    Optional[str]
        The chosen logfile path or None if file creation failed.
    """
    for path in (LOG_PATH_PRIMARY, LOG_PATH_FALLBACK):
        try:
            with open(path, "a+", encoding="utf-8"):
                pass
            return path
        except Exception:
            continue
    return None


def configure_logger(path):
    """
    Configure the logger for console and rotating file output.

    Parameters
    ----------
    path : Optional[str]
        File path for rotating log handler (if available).
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers = []  # avoid duplicates on multiple invocations

    formatter = logging.Formatter("%(asctime)s :: %(levelname)s :: %(message)s")

    # Console handler
    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setFormatter(formatter)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    # Rotating file handler
    if LOG_TO_FILE and path:
        try:
            fh = RotatingFileHandler(
                path, mode="a", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8"
            )
            fh.setFormatter(formatter)
            fh.setLevel(logging.INFO)
            logger.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"Non-fatal: could not attach file logger: {exc}")


def log_info(message):
    """
    Log an INFO-level message.

    Parameters
    ----------
    message : str
        Message to log.
    """
    logging.getLogger(__name__).info(str(message))


def log_critical(message):
    """
    Log a CRITICAL-level message.

    Parameters
    ----------
    message : str
        Message to log.
    """
    logging.getLogger(__name__).critical(str(message))


# =============================================================================
# Log upload (TFTP PUT)
# =============================================================================

def _os_path_to_cli_path(os_path):
    """
    Convert a Guestshell OS path (e.g., /flash/guest-share/ztp.log) into an
    IOS CLI-readable path (e.g., flash:guest-share/ztp.log).

    Returns None if the path is empty.
    """
    if not os_path:
        return None

    # Most common Guestshell mounts
    if os_path.startswith('/flash/guest-share/'):
        return 'flash:guest-share/' + os_path.split('/flash/guest-share/', 1)[1]
    if os_path.startswith('/flash/'):
        # /flash/<file> maps to flash:<file>
        return 'flash:' + os_path.split('/flash/', 1)[1]

    # Best-effort: if caller already supplied an IOS path, pass through
    if os_path.startswith('flash:') or os_path.startswith('bootflash:'):
        return os_path

    # Fallback: treat as a plain filename on flash:
    return 'flash:' + os_path.rsplit('/', 1)[-1]


def _close_file_log_handlers():
    """
    Flush and close any RotatingFileHandler attached to our logger.

    This helps ensure the logfile on flash is complete and not being written to
    while we upload it.
    """
    logger = logging.getLogger(__name__)
    for handler in list(logger.handlers):
        try:
            if isinstance(handler, RotatingFileHandler):
                handler.flush()
                handler.close()
                logger.removeHandler(handler)
        except Exception:
            # Non-fatal
            pass


def upload_logfile_to_tftp(log_os_path, facts=None):
    """
    Upload the local ZTP logfile to the TFTP server at script completion.

    Parameters
    ----------
    log_os_path : str
        Guestshell OS path to the logfile (e.g., /flash/guest-share/ztp.log).
    facts : dict, optional
        Device facts dict used for naming (serial/status).

    Returns
    -------
    bool
        True if the IOS 'copy' reports success; False otherwise.
    """
    if not ENABLE_LOG_UPLOAD:
        return False
    if not log_os_path:
        return False

    facts = facts or {}
    serial = facts.get('serial') or 'UNKNOWN'
    status = facts.get('ztp_status') or 'SUCCESS'
    epoch = int(time.time())

    remote_name = LOG_UPLOAD_NAME_TEMPLATE.format(serial=serial, status=status, epoch=epoch)
    remote_dir = (LOG_UPLOAD_REMOTE_DIR or '').strip('/')
    server = LOG_UPLOAD_SERVER or TFTP_SERVER

    local_cli = _os_path_to_cli_path(log_os_path)
    if not local_cli:
        return False

    url = f"tftp://{server}/{remote_name}" if not remote_dir else f"tftp://{server}/{remote_dir}/{remote_name}"

    # Use the same retry/backoff logic as downloads
    attempt = 0
    while attempt < MAX_COPY_ATTEMPTS:
        attempt += 1
        try:
            # Print (not log) to avoid re-opening the file handler after we close it
            print(f"#### Log upload attempt {attempt}/{MAX_COPY_ATTEMPTS}: {local_cli} -> {url} ####")
            out = cli(f"copy {local_cli} {url}")
            if is_copy_success(str(out)):
                print('#### Logfile uploaded to TFTP successfully ####')
                return True
            print('Log upload output did not indicate success; will retry.')
            print(str(out))
        except Exception as exc:  # noqa: BLE001
            print(f"Log upload attempt {attempt} raised exception: {exc}")

        sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        time.sleep(min(sleep_s, 30))

    print('#### Logfile upload to TFTP failed after retries ####')
    return False


# =============================================================================
# Report composition
# =============================================================================

def compose_report_text(facts):
    """
    Compose a plain-text ZTP report with key device facts.

    Parameters
    ----------
    facts : dict
        Dictionary of device facts (serial, hostname, status, etc.).

    Returns
    -------
    str
        Plain-text report content.
    """
    lines = [
        f"ZTP Status       : {facts.get('ztp_status','SUCCESS')}",
        f"Hostname         : {facts.get('hostname','')}",
        f"Serial           : {facts.get('serial','')}",
        f"Model            : {facts.get('model','')}",
        f"Software         : {facts.get('version','')}",
        f"Mgmt IP (VRF)    : {facts.get('ip','')} ({facts.get('vrf') or 'default'})",
        f"Time (device)    : {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())}",
    ]
    err = facts.get("error", "")
    if err:
        lines.append("")
        lines.append("Error:")
        lines.append(err)
    return "\n".join(lines) + "\n"


def append_report_to_log(facts):
    """Append ZTP summary report into the existing logfile (single artifact)."""
    try:
        log_info("========== ZTP SUMMARY REPORT ==========")
        report = compose_report_text(facts)
        for line in str(report).splitlines():
            log_info(line)
        log_info("========== END ZTP SUMMARY REPORT ======")
    except Exception as exc:  # noqa: BLE001
        try:
            log_info("Non-fatal: could not append report to logfile: " + str(exc))
        except Exception:
            pass


# =============================================================================
# Module guard
# =============================================================================

if __name__ == "__main__":
    main()
