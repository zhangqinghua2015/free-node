#!/usr/bin/env python3
"""
Extract password from YouTube video hard subtitles using RapidOCR.

Usage:
  python yt.py                          # use default CHANNEL_URL
  python yt.py <channel_url>            # specify channel URL
  python yt.py <video_url> --video      # specify a video URL directly

Environment variables (optional):
  PASSWORD_SEGMENT : "start-end" in seconds, e.g. "120-180" to scan 2:00-3:00.
  OCR_REGION       : normalized coordinates "x,y,w,h" e.g. "0.7,0.8,0.3,0.2".
                     Default scans bottom portion of the frame.
"""

import os
import re
import sys
import argparse
import subprocess
import datetime
import tempfile
import shutil
from pathlib import Path

import base64
import hashlib
import hmac as hmac_mod
import json as json_mod
import zlib

import requests
import yaml


CHANNELS = {
    "jcnode": "https://www.youtube.com/@jcnode",
    "QFZYFX": "https://www.youtube.com/@QFZYFX/videos",
}

# Channel-specific ROI: only scan the region where each channel shows passwords
CHANNEL_ROI = {
    "jcnode":  ("jcnode_middle",       "0.2,0.6,0.6,0.35"),
    "QFZYFX":  ("qfzyfx_bottom_wide",  "0,0.85,1,0.20"),
}

DEFAULT_CHANNEL = "https://www.youtube.com/@jcnode"
COOKIES_FILE = "cookies.txt"

PASSWORD_PATTERNS = [
    r'口令[：:]\s*(\d+)',
    r'口令\s*[是为]\s*(\d+)',
    r'口令\s*(\d+)',
    r'令[是为]\s*(\d+)',
    r'令\s*[：:]\s*(\d+)',
    r'令\s*(\d{3,})',
    r'率码[是为]\s*(\d+)',
    r'离码[是为]\s*(\d+)',
    r'密码\s*[是为]\s*(\d+)',
    r'密码\s*(\d+)',
    r'率碍\s*[是为]?\s*(\d+)',
    r'密碍\s*[是为]?\s*(\d+)',
    r'离碍\s*[是为]?\s*(\d+)',
    r'码\s*[是为]?\s*(\d+)',
    r'节点多\s*(\d+)',
    r'节点率\s*(\d+)',
    r'节点是\s*(\d+)',
    r'节点的\s*(\d+)',
    r'节点密\s*(\d+)',
    r'节点.\s*(\d+)',

]


# Custom YAML string class for enforcing single quotes
class QuotedString(str):
    """Custom string class that YAML will render with single quotes"""
    pass


def _add_cookies(ydl_opts, cookies_file):
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
        return True
    return False


def get_latest_video_url(channel_url, cookies_file=None):
    import yt_dlp
    print(f"[DEBUG] Fetching latest video from: {channel_url}")
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'playlistend': 1,
        'js_runtimes': {'node': {}},
    }
    cookies_added = _add_cookies(ydl_opts, cookies_file)
    print(f"[DEBUG] Cookies file present? {cookies_added} (path: {cookies_file})")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if 'entries' in info and info['entries']:
                first = info['entries'][0]
                print(f"[DEBUG] Found video: {first.get('title', 'N/A')[:80]}")
                print(f"[DEBUG]   id={first.get('id', 'N/A')}")
                video_url = f"https://youtube.com/watch?v={first['id']}"
                return video_url, first.get('title', ''), first.get('description', '')
            else:
                print("[DEBUG] No entries found in channel feed.")
    except Exception as e:
        print(f"[ERROR] get_latest_video_url: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    return None, None, None


def get_video_description(video_url, cookies_file=None):
    """Fetch full video description (extract_flat doesn't include it)."""
    import yt_dlp
    print(f"[DEBUG] Fetching full video info for description: {video_url}")
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'js_runtimes': {'node': {}},
    }
    _add_cookies(ydl_opts, cookies_file)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return info.get('description', '')
    except Exception as e:
        print(f"[ERROR] get_video_description: {e}")
    return ''


def extract_from_text(text):
    if not text:
        return None
    for pat in PASSWORD_PATTERNS:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def download_video(video_url, video_path, cookies_file=None):
    import yt_dlp
    base_opts = {
        'outtmpl': str(video_path),
        'quiet': False,
        'no_warnings': False,
        'ignore_no_formats_error': True,
        'merge_output_format': 'mp4',
        'js_runtimes': {'node': {}},
        'remote_components': ['ejs:github'],
        'extractor_args': {
            'youtube': {
                'player_client': ['web'],
            }
        }
    }
    _add_cookies(base_opts, cookies_file)

    format_attempts = [
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        "best[ext=mp4]",
        "best",
    ]

    for fmt in format_attempts:
        opts = base_opts.copy()
        opts['format'] = fmt
        try:
            ydl = yt_dlp.YoutubeDL(opts)
            print(f"Attempting download with format: {fmt}")
            ydl.download([video_url])
            if video_path.exists() and video_path.stat().st_size > 0:
                print(f"Success with format: {fmt}")
                return True
        except Exception as e:
            print(f"Format {fmt} failed: {e}")
            continue

    print("All format attempts failed.")
    return False


def get_video_duration(video_path):
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
        capture_output=True, text=True
    )
    duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
    return duration


def extract_frames(video_path, frames_dir, start, end, fps, prefix):
    if start >= end:
        return 0
    cmd = [
        'ffmpeg', '-i', str(video_path),
        '-ss', str(start), '-to', str(end),
        '-vf', f'fps={fps}',
        f'{frames_dir}/{prefix}_%04d.png',
        '-y', '-loglevel', 'error'
    ]
    subprocess.run(cmd, check=False)
    count = len(list(Path(frames_dir).glob(f"{prefix}_*.png")))
    return count


def crop_roi(img, roi_cfg):
    x0, y0, w, h = map(float, roi_cfg.split(','))
    x0 = int(x0 * img.width)
    y0 = int(y0 * img.height)
    w = int(w * img.width)
    h = int(h * img.height)
    return img.crop((x0, y0, x0 + w, y0 + h))


def ocr_scan_frames(frames_dir, roi_cfg, ocr_engine, debug_save=False):
    import numpy as np
    from PIL import Image
    frame_files = sorted(Path(frames_dir).glob("*.png"))
    print(f"[OCR] Scanning {len(frame_files)} frames, ROI={roi_cfg}")
    for idx, frame_file in enumerate(frame_files):
        img = Image.open(frame_file)
        roi = crop_roi(img, roi_cfg)
        arr = np.array(roi)
        result, _ = ocr_engine(arr)
        texts = []
        if result:
            for item in result:
                texts.append(item[1])
        text = " ".join(texts)
        if text:
            print(f"[OCR] {frame_file.name}: {text[:150]}")
        password = extract_from_text(text)
        if password:
            print(f"[FOUND] {frame_file.name}: {password}")
            return password
        if debug_save and idx < 5:
            debug_dir = Path.cwd() / "debug_roi"
            debug_dir.mkdir(exist_ok=True)
            roi.save(debug_dir / f"original_{frame_file.name}")
    return None


def detect_channel(url):
    """Detect channel alias from URL, returns None if unknown."""
    url_lower = url.lower()
    for name, ch_url in CHANNELS.items():
        if name.lower() in url_lower or ch_url.lower().rstrip('/').split('/')[-1] in url_lower:
            return name
    return None

def ocr_password_from_video(video_url, temp_dir, cookies_file=None, channel=None):
    video_path = Path(temp_dir) / "video.mp4"

    if not download_video(video_url, video_path, cookies_file):
        return None

    duration = get_video_duration(video_path)
    if duration <= 0:
        print("Unable to determine video duration.")
        return None
    print(f"[INFO] Video duration: {duration:.1f}s")

    frames_dir = Path(temp_dir) / "frames"
    frames_dir.mkdir(exist_ok=True)

    segment_env = os.environ.get("PASSWORD_SEGMENT", "")
    if segment_env and "-" in segment_env:
        scan_start, scan_end = max(0, int(segment_env.split("-")[0])), min(duration, int(segment_env.split("-")[1]))
    else:
        scan_start = 70
        scan_end = min(140, duration)

    print(f"[FRAME] Extracting {scan_start:.0f}s - {scan_end:.0f}s at 1fps")
    count = extract_frames(video_path, frames_dir, scan_start, scan_end, 1.0, "f")
    print(f"[FRAME] Extracted {count} frames")

    if count > 0:
        from rapidocr_onnxruntime import RapidOCR
        debug_save = os.environ.get("OCR_DEBUG", "") == "1"
        ocr_engine = RapidOCR()
        rois = [CHANNEL_ROI[channel]] if channel and channel in CHANNEL_ROI else [
            ("qfzyfx_bottom_wide", "0,0.85,1,0.20"),
            ("jcnode_middle", "0.2,0.6,0.6,0.35"),
        ]
        for roi_name, roi_cfg in rois:
            print(f"[SCAN] ROI={roi_name}")
            password = ocr_scan_frames(frames_dir, roi_cfg, ocr_engine, debug_save=debug_save)
            if password:
                return password

    return None


JCNODE_VERIFY_URL = "https://jcnode.com/api/verify"


def _http_post_json(url, payload, retries=3):
    """POST JSON with retry and SSL fallback."""
    import urllib3
    urllib3.disable_warnings()
    for attempt in range(retries):
        try:
            resp = requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError:
            # Retry without SSL verification
            try:
                resp = requests.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=15, verify=False,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                print(f"[WARN] Attempt {attempt+1}/{retries} SSL fallback failed: {e}")
        except requests.RequestException as e:
            print(f"[WARN] Attempt {attempt+1}/{retries} failed: {e}")
    return None


def _http_get_json(url, retries=3):
    """GET JSON with retry and SSL fallback."""
    import urllib3
    urllib3.disable_warnings()
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={"Accept": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError:
            try:
                resp = requests.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=20, verify=False,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                print(f"[WARN] Attempt {attempt+1}/{retries} SSL fallback failed: {e}")
        except requests.RequestException as e:
            print(f"[WARN] Attempt {attempt+1}/{retries} failed: {e}")
    return None


def fetch_jcnode_subscription(password):
    """Fetch subscription links from jcnode using the password."""
    print(f"[FETCH] Submitting password to jcnode: {password}")
    data = _http_post_json(JCNODE_VERIFY_URL, {"code": password})
    if data is None:
        print("[ERROR] jcnode API request failed after retries")
        return None

    if not data.get("success"):
        print(f"[ERROR] jcnode verify failed: {data.get('message', 'unknown error')}")
        return None

    links = data.get("links", {})
    return links


def print_subscription_links(links):
    """Pretty-print subscription links."""
    print("\n" + "=" * 60)
    print("  订阅链接1（访问时无需代理）")
    print("=" * 60)
    for proto, label in [("clash", "Clash"), ("singbox", "Sing-box"), ("v2ray", "V2Ray")]:
        url = links.get("direct", {}).get(proto, "")
        print(f"  {label:10s}: {url or '链接暂不可用'}")

    print("-" * 60)
    print("  订阅链接2（访问时需要代理）")
    print("-" * 60)
    for proto, label in [("clash", "Clash"), ("singbox", "Sing-box"), ("v2ray", "V2Ray")]:
        url = links.get("proxy", {}).get(proto, "")
        print(f"  {label:10s}: {url or '链接暂不可用'}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# PrivateBin v2 decryption (for QFZYFX subscription links)
# ---------------------------------------------------------------------------

_BASE58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def _base58_decode(s):
    """Decode a base58 string to raw bytes (Bitcoin alphabet)."""
    n = 0
    for ch in s.encode('ascii'):
        n = n * 58 + _BASE58_ALPHABET.index(ch)
    result = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b'\x00'
    # Preserve leading zero bytes (base58 '1' characters)
    leading_zeros = len(s) - len(s.lstrip('1'))
    return b'\x00' * leading_zeros + result


def _privatebin_decrypt(paste_url, password):
    """Decrypt a PrivateBin v2 paste and return the plaintext content.

    Args:
        paste_url: Full PrivateBin URL including fragment key (e.g. https://paste.to/?id#key)
        password: Password string to decrypt the paste
    Returns:
        Decrypted plaintext string, or None on failure.
    """
    from urllib.parse import urlparse, parse_qs
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    parsed = urlparse(paste_url)
    fragment = parsed.fragment
    if not fragment:
        print("[ERROR] No decryption key found in URL fragment")
        return None

    # Paste ID is the query parameter (without the '?' prefix)
    paste_id = parsed.query
    if not paste_id:
        print("[ERROR] No paste ID found in URL")
        return None

    # Fetch encrypted paste data
    api_url = f"{parsed.scheme}://{parsed.netloc}/?{paste_id}"
    print(f"[FETCH] Fetching PrivateBin paste: {paste_id}")
    data = _http_get_json(api_url)
    if data is None:
        print("[ERROR] Failed to fetch paste after retries")
        return None

    if data.get('status') != 0:
        print(f"[ERROR] Paste API returned error status: {data.get('status')}")
        return None

    # Parse adata (authenticated data + encryption spec)
    adata = data['adata']
    spec = adata[0]  # [base64_iv, base64_salt, iterations, key_size, tag_size, algo, mode, compression]
    ct_b64 = data['ct']

    iv = base64.b64decode(spec[0])
    salt = base64.b64decode(spec[1])
    iterations = spec[2]
    key_size = spec[3] // 8  # convert bits to bytes
    tag_size = spec[4] // 8
    compression = spec[7]
    adata_str = json_mod.dumps(adata, separators=(',', ':'))

    # Derive key from fragment + password (matching PrivateBin JS logic)
    key_bytes = _base58_decode(fragment)
    # Pad to 32 bytes (matching .padStart(32, '\x00') in JS)
    if len(key_bytes) < 32:
        key_bytes = b'\x00' * (32 - len(key_bytes)) + key_bytes

    # Concatenate key + password bytes (matching JS string concatenation)
    password = password.strip()
    print(f"[DEBUG] Password: '{password}' ({len(password)} chars)")
    print(f"[DEBUG] Key bytes ({len(key_bytes)}): {key_bytes[:8].hex()}...")
    kdf_input = key_bytes + password.encode('latin-1')

    # PBKDF2 key derivation
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=key_size,
        salt=salt,
        iterations=iterations,
    )
    derived_key = kdf.derive(kdf_input)

    # Decrypt with AES-GCM
    ct_bytes = base64.b64decode(ct_b64)
    aesgcm = AESGCM(derived_key)
    nonce = iv
    associated_data = adata_str.encode('latin-1')

    try:
        plaintext = aesgcm.decrypt(nonce, ct_bytes, associated_data)
    except Exception as e:
        print(f"[ERROR] AES-GCM decryption failed (password='{password}'): {e}")
        return None

    # Decompress if needed (PrivateBin uses raw deflate, not zlib format)
    if compression == 'zlib':
        try:
            plaintext = zlib.decompress(plaintext, -15)  # -15 = raw deflate, no header
        except Exception as e:
            print(f"[ERROR] deflate decompression failed: {e}")
            return None

    return plaintext.decode('utf-8')


def _extract_subscription_url(description):
    """Extract subscription URL from video description (after '订阅地址：' or '订阅地址:')."""
    if not description:
        return None
    print(f"{description}")
    pattern = r'(订阅地址|\(Nodes Link\))[：:]\s.*?\[?(https?://[^\s　（）\[\]]+)'
    match = re.search(pattern, description)
    if match:
        return match.group(2)
    return None


def fetch_qfzyfx_subscription(description, password):
    """Fetch subscription content from QFZYFX channel.

    Extracts the PrivateBin URL from the video description, then decrypts
    the paste using the OCR-extracted password.
    """
    sub_url = _extract_subscription_url(description)
    if not sub_url:
        print("[ERROR] Could not find subscription URL in video description")
        return None

    print(f"[FETCH] Subscription URL: {sub_url}")
    sub_file = Path.cwd() / "QFZYFX_P.txt"
    sub_file.write_text(f"{sub_url}\n{password}", encoding="utf-8")
    print(f"[SAVE] Subscription URL + password saved to {sub_file}")
    plaintext = _privatebin_decrypt(sub_url, password)
    return plaintext


def print_subscription_content(content):
    """Print decrypted subscription content."""
    print("\n" + "=" * 60)
    print("  QFZYFX 订阅信息")
    print("=" * 60)
    print(content)
    print("=" * 60 + "\n")


TEMPLATE_URL = "https://zhangqinghua2015.github.io/web-music/clash/mihomo_template.yaml"


def _parse_vmess(b64_str):
    try:
        decoded = base64.b64decode(b64_str).decode("utf-8")
        obj = json_mod.loads(decoded)
        result = {
            "type": "vmess",
            "server": obj["add"],
            "port": int(obj["port"]),
            "uuid": obj["id"],
            "alterId": obj.get("aid", 0),
            "network": obj.get("net", "tcp"),
        }
        if obj.get("ps"):
            result["name"] = obj["ps"]
        elif obj.get("add"):
            result["name"] = obj["add"]
        if obj.get("scy"):
            result["cipher"] = obj["scy"]
        if obj.get("tls") in ("true", True):
            result["tls"] = True
        return result
    except Exception:
        return None


def _parse_trojan(url):
    try:
        match = re.match(r"trojan://([^@]+)@([^:]+):(\d+)", url)
        if not match:
            return None
        result = {
            "type": "trojan",
            "server": match.group(2),
            "port": int(match.group(3)),
            "password": match.group(1),
        }
        idx = url.find("?")
        if idx > -1:
            from urllib.parse import parse_qs
            params = parse_qs(url[idx:].split("#")[0])
            name_match = re.search(r"#([^#]+)$", url)
            if name_match:
                result["name"] = name_match.group(1)
            if params.get("sni"):
                result["sni"] = params["sni"][0]
            if params.get("allowInsecure"):
                result["skip-cert-verify"] = params["allowInsecure"][0] == "1"
            if params.get("network") and params["network"][0] == "ws":
                result["network"] = "ws"
                ws_opts = {}
                if params.get("path"):
                    ws_opts["path"] = params["path"][0]
                if params.get("host"):
                    ws_opts["headers"] = {"Host": params["host"][0]}
                if ws_opts:
                    result["ws-opts"] = ws_opts
            result["udp"] = True
        return result
    except Exception:
        return None


def _parse_ss(url):
    try:
        match = re.match(r"ss://([A-Za-z0-9+/=]+)@([^:]+):(\d+)", url)
        if not match:
            return None
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
        colon_idx = decoded.index(":")
        result = {
            "type": "ss",
            "server": match.group(2),
            "port": int(match.group(3)),
            "cipher": decoded[:colon_idx],
            "password": decoded[colon_idx + 1:],
        }
        name_match = re.search(r"#([^#]+)$", url)
        if name_match:
            result["name"] = name_match.group(1)
        return result
    except Exception:
        return None


def _parse_vless(url):
    try:
        match = re.match(r"vless://([^@]+)@([^:]+):(\d+)", url)
        if not match:
            return None
        from urllib.parse import parse_qs
        parts = url.split("?")
        params = parse_qs(parts[1]) if len(parts) > 1 else {}
        result = {
            "type": "vless",
            "server": match.group(2),
            "port": int(match.group(3)),
            "uuid": match.group(1),
            "network": params.get("network", ["tcp"])[0],
            "tls": params.get("tls", ["false"])[0] in ("true", "1") or params.get("security", [""])[0] == "tls",
        }
        if params.get("flow"):
            result["flow"] = params["flow"][0]
        if params.get("sni"):
            result["servername"] = params["sni"][0]
        if params.get("path"):
            result["ws-opts"] = {
                "path": params["path"][0],
                "headers": {"Host": params.get("host", [""])[0]},
            }
        name_match = re.search(r"#([^#]+)$", url)
        if name_match:
            result["name"] = name_match.group(1)
        return result
    except Exception:
        return None


def _parse_line_to_proxy(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith("vmess://"):
        return _parse_vmess(line[8:])
    if line.startswith("trojan://"):
        return _parse_trojan(line)
    if line.startswith("ss://"):
        return _parse_ss(line)
    if line.startswith("vless://"):
        return _parse_vless(line)
    return None


def _try_parse_content(content):
    """Try to parse subscription content into a dict with 'proxies' key."""
    content = content.strip()

    # 1. Try as direct YAML
    try:
        data = yaml.safe_load(content)
        if isinstance(data, dict) and "proxies" in data:
            return data
    except Exception:
        pass

    # 2. Try as base64-encoded YAML
    try:
        decoded = base64.b64decode(content).decode("utf-8")
        data = yaml.safe_load(decoded)
        if isinstance(data, dict) and "proxies" in data:
            return data
    except Exception:
        pass

    # 3. Try as base64-encoded + URL-decoded proxy URL list
    try:
        from urllib.parse import unquote
        decoded = base64.b64decode(content).decode("utf-8")
        decoded = unquote(decoded)
        lines = [
            l for l in decoded.splitlines()
            if l.strip().startswith(("vmess://", "trojan://", "ss://", "vless://"))
        ]
        if lines:
            proxies = [p for p in (_parse_line_to_proxy(l) for l in lines) if p]
            if proxies:
                return {"proxies": proxies}
    except Exception:
        pass

    return None


def _setup_quoted_string_representer():
    """Register the custom representer for QuotedString once at module load"""
    def represent_quoted_string(dumper, data):
        return dumper.represent_scalar('tag:yaml.org,2002:str', str(data), style="'")
    
    yaml.add_representer(QuotedString, represent_quoted_string)


def _fix_reality_short_id(proxies):
    """Fix reality-opts short-id: add single quotes if missing."""
    for proxy in proxies:
        if isinstance(proxy, dict) and "reality-opts" in proxy:
            reality_opts = proxy["reality-opts"]
            if isinstance(reality_opts, dict) and "short-id" in reality_opts:
                short_id = reality_opts["short-id"]
                # Check if short-id is not already quoted (e.g., string wrapped in quotes)
                # If it's a plain value without quotes, wrap it
                if isinstance(short_id, str) and not (short_id.startswith("'") and short_id.endswith("'")):
                    reality_opts["short-id"] = QuotedString(short_id)
                    print(f"[INFO] Fixed reality-opts short-id for proxy '{proxy.get('name', 'unknown')}': {short_id} -> '{short_id}'")


def _merge_template(template, data):
    """Merge proxies and proxy-groups into the template."""
    proxies = data["proxies"]
    proxies.sort(key=lambda p: p.get("name", ""))

    # Fix reality-opts short-id
    _fix_reality_short_id(proxies)

    template["proxies"] = proxies

    proxy_groups = data.get("proxy-groups")
    if proxy_groups:
        names_to_remove = {p["name"] for p in proxy_groups}
        template["proxy-groups"] = [
            g for g in template["proxy-groups"] if g["name"] not in names_to_remove
        ]
        for g in template["proxy-groups"]:
            if g["name"] in ("🚀 节点选择", "🌍 国外媒体", "📲 电报信息", "Ⓜ️ 微软服务", "🍎 苹果服务"):
                g["proxies"].extend(p["name"] for p in proxy_groups)
        template["proxy-groups"].extend(proxy_groups)
    else:
        auto_group = {
            "name": "♻️ 自动选择",
            "type": "url-test",
            "proxies": [p["name"] for p in proxies],
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        }
        template["proxy-groups"].append(auto_group)
        for g in template["proxy-groups"]:
            if g["name"] == "🚀 节点选择":
                g["proxies"].extend(p["name"] for p in proxies)
            if g["name"] in ("🌍 国外媒体", "📲 电报信息", "Ⓜ️ 微软服务", "🍎 苹果服务"):
                g["proxies"].append("♻️ 自动选择")

    return template


def convert_clash_url(clash_url):
    """Fetch subscription URL, parse proxies, merge into template, return YAML string."""
    print(f"[CONVERT] Fetching subscription: {clash_url}")
    headers = {
        "User-Agent": "clash-verge/v2.0.0",
        "Accept": "*/*",
    }
    try:
        resp = requests.get(clash_url, timeout=30, headers=headers)
        resp.raise_for_status()
        content = resp.content.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[ERROR] Failed to fetch subscription: {e}")
        return None

    data = _try_parse_content(content)
    if not data or not data.get("proxies"):
        print("[ERROR] No proxies found in subscription content")
        return None

    print(f"[CONVERT] Parsed {len(data['proxies'])} proxies")

    print(f"[CONVERT] Fetching template: {TEMPLATE_URL}")
    try:
        resp = requests.get(TEMPLATE_URL, timeout=15)
        resp.raise_for_status()
        template = yaml.safe_load(resp.content.decode("utf-8"))
    except Exception as e:
        print(f"[ERROR] Failed to fetch template: {e}")
        return None

    template = _merge_template(template, data)
    result = yaml.dump(template, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"[CONVERT] Generated YAML ({len(result)} bytes)")
    return result


def save_clash_yaml(yaml_content, filename):
    """Save YAML content to a .yaml file."""
    path = Path.cwd() / f"{filename}.yaml"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = yaml_content.rstrip("\n") + f"\n# updated: {now}\n"
    path.write_text(content, encoding="utf-8")
    print(f"[SAVE] Saved to {path}")
    return path


def git_commit_and_push(files):
    """Commit the given files and push to remote."""
    if isinstance(files, (str, Path)):
        files = [str(files)]
    files_to_add = [f for f in files if Path(f).exists()]
    if not files_to_add:
        print("[GIT] No files to commit")
        return
    try:
        for f in files_to_add:
            subprocess.run(["git", "add", f], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], capture_output=True
        )
        if result.returncode == 0:
            print("[GIT] No changes to commit")
            return
        today = datetime.date.today().isoformat()
        msg = f"Update {', '.join(files_to_add)} [{today}]"
        subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
        print(f"[GIT] Committed: {msg}")
        result = subprocess.run(
            ["git", "push"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print("[GIT] Pushed to remote")
        else:
            print(f"[GIT] Push failed: {result.stderr.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"[GIT] Error: {e.stderr.strip() if e.stderr else e}")
    except subprocess.TimeoutExpired:
        print("[GIT] Push timed out")


def git_checkout_file(filepath):
    """Restore a file to its last committed state."""
    subprocess.run(["git", "checkout", "HEAD", "--", filepath], capture_output=True)
    print(f"[GIT] Restored {filepath}")


def extract_clash_url(channel, data):
    """Extract Clash subscription URL from channel data.

    Args:
        channel: "jcnode" or "QFZYFX"
        data: For jcnode, the links dict; for QFZYFX, the decrypted plaintext string
    Returns:
        Clash subscription URL string, or None.
    """
    if channel == "jcnode":
        links = data
        return links.get("direct", {}).get("clash") or links.get("proxy", {}).get("clash")

    if channel == "QFZYFX":
        text = data
        # Try to parse as JSON first (PrivateBin content is usually JSON)
        try:
            obj = json_mod.loads(text)
            text = obj.get("paste", text)
        except (json_mod.JSONDecodeError, AttributeError):
            pass
        # Match patterns like "Clash、Mate订阅链接：\nhttps://..."
        match = re.search(r'Clash[、,，].*?订阅链接[：:]\s*\n?(https?://\S+)', text)
        if match:
            return match.group(1)
        # Fallback: any URL with "clash" in query/path
        match = re.search(r'(https?://\S*?clash\S*)', text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def convert_from_txt(channel):
    """Read URL from {channel}.txt (or QFZYFX_P.txt for QFZYFX), convert to Clash YAML, commit and push."""
    if channel == "QFZYFX":
        p_file = Path.cwd() / "QFZYFX_P.txt"
        if not p_file.exists():
            print(f"[ERROR] {p_file} not found")
            return False
        lines = p_file.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) < 2:
            print(f"[ERROR] {p_file} must contain URL and password on separate lines")
            return False
        sub_url, password = lines[0].strip(), lines[1].strip()
        print(f"[CONVERT] QFZYFX PrivateBin URL: {sub_url}")
        content = _privatebin_decrypt(sub_url, password)
        if not content:
            print("[ERROR] Failed to decrypt PrivateBin paste")
            return False
        clash_url = extract_clash_url(channel, content)
    else:
        txt_file = Path.cwd() / f"{channel}.txt"
        if not txt_file.exists():
            print(f"[ERROR] {txt_file} not found")
            return False
        clash_url = txt_file.read_text(encoding="utf-8").strip()
        if not clash_url:
            print(f"[ERROR] {txt_file} is empty")
            return False
    print(f"[CONVERT] Channel={channel}, Clash URL={clash_url}")
    if not clash_url:
        print("[ERROR] Failed to extract Clash subscription URL")
        return False
    yaml_content = convert_clash_url(clash_url)
    if not yaml_content:
        print("[ERROR] Failed to convert subscription URL")
        return False
    save_clash_yaml(yaml_content, channel)
    yaml_file = f"{channel}.yaml"
    git_commit_and_push(yaml_file)
    return True


def main():
    parser = argparse.ArgumentParser(description='Extract password from YouTube video subtitles')
    parser.add_argument('url', nargs='?', default=None, help='Channel or video URL')
    parser.add_argument('--video', action='store_true', help='Treat URL as a direct video URL')
    parser.add_argument('--channels', action='store_true', help='List available channel aliases')
    parser.add_argument('--no-fetch', action='store_true', help='Skip fetching subscription links (only extract password)')
    parser.add_argument('--convert-only', metavar='CHANNEL', help='Only convert: read URL from {CHANNEL}.txt, fetch and generate Clash YAML')
    args = parser.parse_args()

    if args.channels:
        for name, url in CHANNELS.items():
            print(f"  {name:12s} -> {url}")
        sys.exit(0)

    if args.convert_only:
        success = convert_from_txt(args.convert_only)
        sys.exit(0 if success else 1)

    if args.url:
        channel_url = CHANNELS.get(args.url, args.url)
    elif os.environ.get("CHANNEL_URL"):
        channel_url = os.environ["CHANNEL_URL"]
    else:
        channel_url = DEFAULT_CHANNEL

    channel = detect_channel(channel_url)
    print(f"[INFO] Detected channel: {channel or 'unknown'}")

    if not os.path.exists(COOKIES_FILE):
        print("WARNING: cookies.txt not found. YouTube may block the request.")

    temp_dir = tempfile.mkdtemp()
    print(f"[INFO] Temp directory: {temp_dir}")
    try:
        description = None
        if args.video:
            video_url = channel_url
            title = "direct video"
        else:
            video_url, title, description = get_latest_video_url(channel_url, COOKIES_FILE)
            if not video_url:
                print("ERROR: Could not fetch latest video URL")
                sys.exit(1)

        print(f"Processing: {title}\n{video_url}")

        password = ocr_password_from_video(video_url, temp_dir, COOKIES_FILE, channel=channel)
        if password:
            print(f"password={password}")
            clash_url = None
            txt_file = Path.cwd() / ("QFZYFX_P.txt" if channel == "QFZYFX" else f"{channel or 'unknown'}.txt")
            if channel == "jcnode" and not args.no_fetch:
                links = fetch_jcnode_subscription(password)
                if links:
                    print_subscription_links(links)
                    clash_url = extract_clash_url(channel, links)
            elif channel == "QFZYFX" and not args.no_fetch:
                if not description:
                    description = get_video_description(video_url, COOKIES_FILE)
                content = fetch_qfzyfx_subscription(description, password)
                if content:
                    print_subscription_content(content)
                    clash_url = extract_clash_url(channel, content)
            if clash_url:
                print(f"clash_url={clash_url}")
                yaml_content = convert_clash_url(clash_url)
                if yaml_content:
                    git_checkout_file(txt_file)
                    save_name = channel or "clash"
                    save_clash_yaml(yaml_content, save_name)
                    git_commit_and_push(f"{save_name}.yaml")
                else:
                    if channel != "QFZYFX":
                        txt_file.write_text(clash_url, encoding="utf-8")
                        print(f"[SAVE] Clash URL saved to {txt_file}")
                    git_commit_and_push(txt_file)
            else:
                if txt_file.exists():
                    git_commit_and_push(txt_file)
        else:
            print("password=")
            sys.exit(1)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    # Setup YAML representer at startup
    _setup_quoted_string_representer()
    
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True,
    )
    if result.returncode == 0 and "Already up to date" not in result.stdout:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    main()
