#!/usr/bin/env python3
"""
Build and sign a standalone Android WebView APK for WhatsApp AI Companion.
Produces a ready-to-install `dashboard/WhatsApp_Companion.apk` with automatic
tunnel URL resolution and offline fallbacks.
"""
import os
import sys
import re
import json
import zipfile
import hashlib
import base64
import tempfile
import subprocess
import urllib.request

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DASHBOARD_DIR = os.path.join(PROJECT_DIR, "dashboard")
OUTPUT_APK = os.path.join(DASHBOARD_DIR, "WhatsApp_Companion.apk")
CACHE_BASE_APK = os.path.join(PROJECT_DIR, "bin", "base_webview.apk")

# 1. Determine Public or Local URL
def get_target_url():
    tunnel_log = os.path.join(PROJECT_DIR, "logs", "tunnel.log")
    if os.path.exists(tunnel_log):
        with open(tunnel_log, "r", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r'https://[-a-zA-Z0-9@:%._\+~#=]*\.trycloudflare\.com', content)
            if matches:
                return matches[-1]
    return "http://10.0.2.2:8000"  # Android emulator default localhost

# 2. Ensure Base APK template
def ensure_base_apk():
    os.makedirs(os.path.join(PROJECT_DIR, "bin"), exist_ok=True)
    if not os.path.exists(CACHE_BASE_APK) or os.path.getsize(CACHE_BASE_APK) < 100000:
        base_url = "https://github.com/mikespub-archive/android-webview/releases/download/v1.28/app-release.apk"
        print(f"Downloading base WebView APK from {base_url}...")
        urllib.request.urlretrieve(base_url, CACHE_BASE_APK)
        print("Base APK downloaded successfully.")
    return CACHE_BASE_APK

def build_apk():
    base_apk = ensure_base_apk()
    target_url = get_target_url()
    print(f"Targeting Companion URL: {target_url}")

    with tempfile.TemporaryDirectory() as td:
        extracted_dir = os.path.join(td, "extracted")
        os.makedirs(extracted_dir, exist_ok=True)

        # Extract base APK
        with zipfile.ZipFile(base_apk, "r") as z:
            z.extractall(extracted_dir)

        # Remove old signatures
        meta_inf = os.path.join(extracted_dir, "META-INF")
        if os.path.exists(meta_inf):
            for f in os.listdir(meta_inf):
                if f.endswith(".SF") or f.endswith(".RSA") or f.endswith(".MF"):
                    os.remove(os.path.join(meta_inf, f))
        else:
            os.makedirs(meta_inf, exist_ok=True)

        # Customize assets/settings.json
        settings_path = os.path.join(extracted_dir, "assets", "settings.json")
        settings_data = {
            "sites": [
                {
                    "url": target_url,
                    "title": "WhatsApp AI Companion"
                }
            ],
            "other": target_url,
            "match": [
                ["host", "contains", "trycloudflare.com"],
                ["host", "contains", "127.0.0.1"],
                ["host", "contains", "10.0.2.2"],
                ["host", "contains", "192.168."]
            ],
            "remote_debug": True,
            "console_log": True,
            "js_interface": True,
            "local_sites": False,
            "web_settings": {
                "JavaScriptEnabled": True,
                "DomStorageEnabled": True,
                "DatabaseEnabled": True
            }
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings_data, f, indent=2)

        # Customize assets/local/index.html to immediately redirect to target_url
        redirect_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WhatsApp AI Companion</title>
  <style>
    body {{
      background: #090d16;
      color: #f8fafc;
      font-family: -apple-system, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100vh;
      margin: 0;
      text-align: center;
    }}
    .spinner {{
      width: 44px;
      height: 44px;
      border: 4px solid rgba(16, 185, 129, 0.2);
      border-top-color: #10b981;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin-bottom: 20px;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div class="spinner"></div>
  <h2 style="margin: 0; font-size: 1.2rem; font-weight: 700;">WhatsApp Companion</h2>
  <p style="color: #94a3b8; font-size: 0.88rem; margin-top: 8px;">Connecting to your AI bot...</p>
  <script>
    window.location.replace("{target_url}");
  </script>
</body>
</html>"""
        for sub in ["local", "web", "demo"]:
            idx = os.path.join(extracted_dir, "assets", sub, "index.html")
            if os.path.exists(idx):
                with open(idx, "w", encoding="utf-8") as f:
                    f.write(redirect_html)

        # 3. Generate Signatures (v1 JAR Signature)
        # Collect all files to sign
        files_to_sign = []
        for root, _, files in os.walk(extracted_dir):
            for file in files:
                rel = os.path.relpath(os.path.join(root, file), extracted_dir)
                if not rel.startswith("META-INF"):
                    files_to_sign.append(rel)
        files_to_sign.sort()

        # Build MANIFEST.MF
        manifest_lines = [
            "Manifest-Version: 1.0",
            "Created-By: 1.0 (WhatsApp AI Companion Builder)",
            ""
        ]
        manifest_entries = {}
        for rel in files_to_sign:
            file_path = os.path.join(extracted_dir, rel)
            with open(file_path, "rb") as f:
                digest = base64.b64encode(hashlib.sha256(f.read()).digest()).decode()
            entry = f"Name: {rel}\r\nSHA-256-Digest: {digest}\r\n\r\n"
            manifest_entries[rel] = entry
            manifest_lines.append(f"Name: {rel}")
            manifest_lines.append(f"SHA-256-Digest: {digest}")
            manifest_lines.append("")

        manifest_content = "\r\n".join(manifest_lines) + "\r\n"
        manifest_path = os.path.join(meta_inf, "MANIFEST.MF")
        with open(manifest_path, "wb") as f:
            f.write(manifest_content.encode("utf-8"))

        # Build CERT.SF
        manifest_digest = base64.b64encode(hashlib.sha256(manifest_content.encode("utf-8")).digest()).decode()
        sf_lines = [
            "Signature-Version: 1.0",
            "Created-By: 1.0 (WhatsApp AI Companion Builder)",
            f"SHA-256-Digest-Manifest: {manifest_digest}",
            ""
        ]
        for rel in files_to_sign:
            entry_bytes = manifest_entries[rel].encode("utf-8")
            entry_digest = base64.b64encode(hashlib.sha256(entry_bytes).digest()).decode()
            sf_lines.append(f"Name: {rel}")
            sf_lines.append(f"SHA-256-Digest: {entry_digest}")
            sf_lines.append("")

        sf_content = "\r\n".join(sf_lines) + "\r\n"
        sf_path = os.path.join(meta_inf, "CERT.SF")
        with open(sf_path, "wb") as f:
            f.write(sf_content.encode("utf-8"))

        # Sign CERT.SF to create CERT.RSA using openssl
        key_path = os.path.join(td, "key.pem")
        cert_path = os.path.join(td, "cert.pem")
        rsa_path = os.path.join(meta_inf, "CERT.RSA")

        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cert_path,
            "-days", "3650", "-nodes",
            "-subj", "/C=IN/ST=Delhi/L=Delhi/O=WhatsApp Companion/CN=WhatsApp AI Companion"
        ], check=True, capture_output=True)

        subprocess.run([
            "openssl", "smime", "-sign", "-in", sf_path,
            "-out", rsa_path, "-outform", "DER",
            "-inkey", key_path, "-signer", cert_path, "-nodetach"
        ], check=True, capture_output=True)

        # Repackage into OUTPUT_APK
        print(f"Creating signed APK at: {OUTPUT_APK}")
        with zipfile.ZipFile(OUTPUT_APK, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
            for root, _, files in os.walk(extracted_dir):
                for file in files:
                    full_p = os.path.join(root, file)
                    arc_name = os.path.relpath(full_p, extracted_dir)
                    z_out.write(full_p, arc_name)

    size_mb = os.path.getsize(OUTPUT_APK) / (1024 * 1024)
    print(f"✅ Successfully built {OUTPUT_APK} ({size_mb:.2f} MB)")
    return OUTPUT_APK

if __name__ == "__main__":
    build_apk()
