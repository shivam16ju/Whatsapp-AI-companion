#!/usr/bin/env python3
"""
Generate crisp PWA icons (SVG & PNG) for WhatsApp AI Companion without external dependencies.
"""
import os
import zlib
import struct
import math

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")
os.makedirs(DASHBOARD_DIR, exist_ok=True)

# 1. High-resolution SVG icon
SVG_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#064e3b" />
      <stop offset="50%" stop-color="#047857" />
      <stop offset="100%" stop-color="#0f172a" />
    </linearGradient>
    <linearGradient id="bubbleGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#10b981" />
      <stop offset="100%" stop-color="#059669" />
    </linearGradient>
    <linearGradient id="sparkleGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fef08a" />
      <stop offset="50%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="16" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>

  <!-- Background squircle -->
  <rect x="16" y="16" width="480" height="480" rx="112" fill="url(#bgGrad)" stroke="#10b981" stroke-width="6" stroke-opacity="0.4" />

  <!-- Ambient light -->
  <circle cx="256" cy="230" r="140" fill="#10b981" opacity="0.25" filter="url(#glow)" />

  <!-- WhatsApp Chat Bubble Base -->
  <path d="M 256 110 C 173 110 106 177 106 260 C 106 288 114 315 129 337 L 115 395 L 176 381 C 200 397 227 406 256 406 C 339 406 406 339 406 260 C 406 177 339 110 256 110 Z" 
        fill="url(#bubbleGrad)" filter="url(#glow)" />

  <!-- Inner Dark Glass Bubble -->
  <path d="M 256 126 C 182 126 122 186 122 260 C 122 285 129 309 143 329 L 132 376 L 181 365 C 203 379 229 387 256 387 C 330 387 390 327 390 260 C 390 186 330 126 256 126 Z" 
        fill="#091322" opacity="0.9" />

  <!-- AI Sparkle Core (4-point star) -->
  <path d="M 256 180 Q 256 250 186 250 Q 256 250 256 320 Q 256 250 326 250 Q 256 250 256 180 Z" 
        fill="url(#sparkleGrad)" filter="url(#glow)" />

  <!-- Neural Connection Nodes -->
  <circle cx="210" cy="204" r="10" fill="#38bdf8" />
  <circle cx="302" cy="204" r="10" fill="#10b981" />
  <circle cx="210" cy="296" r="10" fill="#10b981" />
  <circle cx="302" cy="296" r="10" fill="#f59e0b" />
  
  <line x1="210" y1="204" x2="256" y2="250" stroke="#38bdf8" stroke-width="3" opacity="0.6" />
  <line x1="302" y1="204" x2="256" y2="250" stroke="#10b981" stroke-width="3" opacity="0.6" />
  <line x1="210" y1="296" x2="256" y2="250" stroke="#10b981" stroke-width="3" opacity="0.6" />
  <line x1="302" y1="296" x2="256" y2="250" stroke="#f59e0b" stroke-width="3" opacity="0.6" />

  <!-- Small secondary sparkle -->
  <path d="M 330 180 Q 330 205 305 205 Q 330 205 330 230 Q 330 205 355 205 Q 330 205 330 180 Z" 
        fill="#ffffff" opacity="0.9" />
</svg>
"""

with open(os.path.join(DASHBOARD_DIR, "app-icon.svg"), "w", encoding="utf-8") as f:
    f.write(SVG_CONTENT.strip())

with open(os.path.join(DASHBOARD_DIR, "favicon.svg"), "w", encoding="utf-8") as f:
    f.write(SVG_CONTENT.strip())

# 2. Pure Python PNG Generator with squircle and sparkle icon
def generate_png(size):
    raw_data = bytearray()
    center = size / 2.0
    radius = size * 0.44

    for y in range(size):
        raw_data.append(0)  # Filter byte None
        for x in range(size):
            # Squircle calculation
            nx = (x - center) / (center * 0.94)
            ny = (y - center) / (center * 0.94)
            squircle_dist = (nx**4 + ny**4)**0.25

            if squircle_dist > 1.0:
                raw_data.extend([0, 0, 0, 0])
                continue

            # Anti-aliasing border
            alpha = 255
            if squircle_dist > 0.95:
                alpha = int(255 * (1.0 - (squircle_dist - 0.95) / 0.05))

            # Base gradient: #064e3b -> #091322
            ratio = y / float(size)
            r = int(6 * (1 - ratio) + 9 * ratio)
            g = int(78 * (1 - ratio) + 19 * ratio)
            b = int(59 * (1 - ratio) + 34 * ratio)

            # Draw circle / speech bubble approximation
            bx = (x - center) / radius
            by = (y - center * 0.96) / radius
            b_dist = math.sqrt(bx*bx + by*by)

            # Bubble border / glow
            if 0.45 <= b_dist <= 0.78:
                r = min(255, int(r + 16))
                g = min(255, int(g + 160))
                b = min(255, int(b + 110))
            elif b_dist < 0.45:
                # Inside bubble dark glass
                r, g, b = 11, 22, 38

            # Center Star Sparkle: |nx| + |ny| < threshold
            sx = abs(x - center) / (size * 0.22)
            sy = abs(y - center * 0.96) / (size * 0.22)
            star_val = (sx**0.5 + sy**0.5)

            if star_val < 0.8:
                # Bright glowing core
                intensity = 1.0 - (star_val / 0.8)
                r = min(255, int(r + 240 * intensity))
                g = min(255, int(g + 245 * intensity))
                b = min(255, int(b + 200 * intensity))

            raw_data.extend([r, g, b, alpha])

    compressor = zlib.compressobj(level=9)
    idat = compressor.compress(bytes(raw_data)) + compressor.flush()

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

# Generate standard PWA sizes
for icon_size, filename in [(192, "app-icon-192.png"), (512, "app-icon-512.png"), (180, "apple-touch-icon.png")]:
    png_bytes = generate_png(icon_size)
    target_path = os.path.join(DASHBOARD_DIR, filename)
    with open(target_path, "wb") as f:
        f.write(png_bytes)
    print(f"Generated {filename} ({icon_size}x{icon_size})")

print("All icons successfully generated in dashboard/!")
